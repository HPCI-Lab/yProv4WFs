"""
This version includes:
- Path normalization for handling multi-tier nested workflows.
- Complete removal of the Level 0 activity node and its peripheral entities.
"""

import os
import os.path
import uuid
import json
import yaml
import logging
from abc import abstractmethod
from zipfile import ZipFile
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import Any, MutableMapping, MutableSequence, Optional, Set

import streamflow.core.utils
from streamflow.core.provenance import ProvenanceManager
from streamflow.core.workflow import Status, Workflow as StreamFlowWorkflow
from streamflow.core.context import StreamFlowContext
from streamflow.core.persistence import DatabaseLoadingContext
from streamflow.log_handler import logger

from cwl_utils.parser import load_document_by_uri

from yprov4wfs.datamodel.workflow import Workflow
from yprov4wfs.datamodel.task import Task
from yprov4wfs.datamodel.data import Data

# yprov4wfs uses aiosqlite for its DB access, which logs every single sqlite call
# we silence it to reduce the spamming except for warnings
logging.getLogger("aiosqlite").setLevel(logging.WARNING)

def discover_workflow_cwl_files(streamflow_config_path: Optional[str]) -> list[str]:
    """
    Parses the Streamflow configuration file (streamflow.yml) to find the primary
    CWL entrypoint, then recursively crawls all referenced CWL sub-workflows 
    and steps using cwl-utils to resolve dynamic and relative paths.
    """
    main_cwl = None

    if streamflow_config_path and os.path.exists(streamflow_config_path):
        try:
            # Read streamflow.yml to extract the primary CWL workflow file
            with open(streamflow_config_path, 'r') as sf_file:
                sf_data = yaml.safe_load(sf_file)
            
            workflows = sf_data.get("workflows", {})
            if workflows and isinstance(workflows, dict):
                # Target the first defined workflow regardless of its name
                first_workflow_name = next(iter(workflows))
                workflow_data = workflows.get(first_workflow_name, {})
                main_cwl_relative = workflow_data.get("config", {}).get("file")
                
                if main_cwl_relative:
                    # Resolve the relative path against the streamflow.yml folder environment
                    config_dir = os.path.dirname(os.path.abspath(streamflow_config_path))
                    main_cwl = os.path.abspath(os.path.join(config_dir, main_cwl_relative))
        except Exception as e:
            logger.warning(f"YPROV: Error parsing streamflow.yml ({streamflow_config_path}): {e}")

    if not main_cwl or not os.path.exists(main_cwl):
        logger.warning(f"YPROV: Unable to determine a valid primary CWL file from config. Resolved as: {main_cwl}")
        return []

    cwl_files_paths: Set[str] = set()

    def discover_recursive(file_path: str):
        abs_path = os.path.abspath(file_path)
        
        # Avoid infinite loops if a file is referenced multiple times
        if abs_path in cwl_files_paths:
            return
        
        cwl_files_paths.add(abs_path)
        
        try:
            # Convert file system path to standard URI required by cwl-utils
            uri = Path(abs_path).resolve().as_uri()
            doc = load_document_by_uri(uri)
            
            # A CWL file might represent a list of activities or a single workflow
            process_list = doc if isinstance(doc, list) else [doc]
            
            for process in process_list:
                # Target Workflow structures containing explicit execution steps
                if hasattr(process, 'steps') and process.steps:
                    for step in process.steps:
                        if hasattr(step, 'run') and isinstance(step.run, str):
                            parsed = urlparse(step.run)
                            if parsed.scheme in ('file', ''):
                                next_file = unquote(parsed.path)
                                if os.path.exists(next_file):
                                    discover_recursive(next_file)
        except Exception as e:
            logger.warning(f"YPROV: Error parsing graph references for {file_path}: {e}")

    # Start tracking the workflow graph from the primary entrance file
    discover_recursive(main_cwl)
    return list(cwl_files_paths)


class yProv4WFsProvenanceManager(ProvenanceManager):
    def __init__(
        self,
        context: StreamFlowContext,
        db_context: DatabaseLoadingContext,
        workflows: MutableSequence[StreamFlowWorkflow],
    ):
        super().__init__(context, db_context, workflows)
        self.map_file: MutableMapping[str, str] = {}
        self.prov_workflow = None
        self.tasks_by_step_name = {}
        self.computed_cwl_deps = {}  # Store resolved structural dependencies
        logger.info("YPROV: Starting and loading workflows...")
        
    @abstractmethod
    async def get_main_entity(self) -> MutableMapping[str, Any]: ...
    
    @abstractmethod
    async def add_initial_inputs(self, wf_id: int, workflow: Workflow) -> None: ...

    def _get_action_status(self, status: Status) -> str:
        if status == Status.COMPLETED: return "Completed"
        elif status == Status.FAILED: return "Failed"
        elif status in [Status.CANCELLED, Status.SKIPPED]: return "Cancelled or Skipped"
        return "Running"

    def _parse_cwl_for_dependencies(self):
        """
        Scan all CWL files, find the current Workflow comparing
        the executed steps and map the topological dependencies for the graph.
        """
        dependencies = {}

        # Discover all workflow CWL files
        streamflow_config_path = self.map_file.get("config")
        cwl_files = discover_workflow_cwl_files(streamflow_config_path)

        if not cwl_files:
            logger.warning("YPROV: No active CWL files discovered via graph parsing.")
            return dependencies
        
        logger.info(f"YPROV: Discovered CWL files for parsing: {cwl_files}")

        # Map out all short names to their absolute execution paths.
        full_path_map = {}
        for full_path in self.tasks_by_step_name.keys():
            normalized_path = '/' + full_path.lstrip('/')
            short_name = normalized_path.split('/')[-1]
            full_path_map[short_name] = normalized_path
        logger.info(f"Full path map: {full_path_map}")

        for filename in cwl_files:
            try:
                absolute_target_path = os.path.abspath(filename)
                
                with open(absolute_target_path, 'r') as f:
                    data = yaml.safe_load(f)
                if data.get('class') != 'Workflow': 
                    continue

                steps = data.get('steps', {})
                steps_items = steps.items() if isinstance(steps, dict) else [(s['id'], s) for s in steps]

                # Evaluate each step found inside the targeted workflow structure
                for step_id, step_val in steps_items:
                    short_step_name = step_id.split('/')[-1]
                    full_step_name = full_path_map.get(short_step_name)

                    if not full_step_name:
                        continue
                    
                    if full_step_name not in dependencies:
                        dependencies[full_step_name] = []
                    
                    current_prefix = full_step_name.rsplit('/', 1)[0]
                    
                    inputs = step_val.get('in', [])
                    input_list = inputs if isinstance(inputs, list) else [{'source': v} for v in inputs.values()]

                    for inp in input_list:
                        src = inp.get('source') if isinstance(inp, dict) else inp
                        if src:
                            sources = src if isinstance(src, list) else [src]
                            for s in sources:
                                # CASE 1: Standard dependency declared within the same file scope
                                if '/' in s:
                                    parent_short_name = s.split('/')[0]
                                    full_parent_name = f"{current_prefix}/{parent_short_name}" if current_prefix else f"/{parent_short_name}"
                                    if full_parent_name in full_path_map.values() and full_parent_name not in dependencies[full_step_name]:
                                        dependencies[full_step_name].append(full_parent_name)
                                
                                # CASE 2: Nested input boundary fallback
                                elif current_prefix and current_prefix != '/':
                                    if current_prefix.count('/') >= 2:
                                        parent_environment = current_prefix.rsplit('/', 1)[0]
                                        
                                        for short_name, full_path in full_path_map.items():
                                            if (full_path.startswith(parent_environment) and 
                                                not full_path.startswith(current_prefix) and 
                                                full_path != full_step_name):
                                                
                                                if full_path not in dependencies[full_step_name]:
                                                    logger.info(f"Boundary dependency resolved: {full_path} -> {full_step_name}")
                                                    dependencies[full_step_name].append(full_path)
            except Exception as e:
                logger.warning(f"YPROV: Error parsing file {filename}: {e}")
                continue

        logger.info(f"YPROV Dependencies list: {dependencies}")
        return dependencies

    async def populate_prov_workflow(self):
        self.tasks_by_step_name = {}
        
        for wf in self.workflows:
            logger.info(f"Workflow ID {wf.persistent_id}")
            wf_obj = await self.context.database.get_workflow(wf.persistent_id)
            self.prov_workflow = Workflow(wf_obj["name"], f'workflow_{wf_obj["name"]}')
            self.prov_workflow._start_time = streamflow.core.utils.get_date_from_ns(wf_obj["start_time"])
            self.prov_workflow._end_time = streamflow.core.utils.get_date_from_ns(wf_obj["end_time"])
            self.prov_workflow._status = self._get_action_status(Status(wf_obj["status"]))
            self.prov_workflow._engineWMS = 'StreamFlow'
            self.prov_workflow._level = '0'

            if "config" in self.map_file:
                self.prov_workflow._resource_cwl_uri = self.map_file["config"]
            
            all_steps = await self.context.database.get_workflow_steps(wf.persistent_id)
            
            for step in all_steps:
                if "ExecuteStep" not in step["type"]:
                    continue
                
                step_id = step["id"]
                
                inputs = await self.context.database.get_input_ports(step_id)
                outputs = await self.context.database.get_output_ports(step_id)

                for input in inputs:
                    data_in = Data(str(uuid.uuid4()), input["name"])
                    self.prov_workflow.add_input(data_in)
                    data_in.add_consumer(self.prov_workflow._id)
                
                for output in outputs:
                    data_out = Data(str(uuid.uuid4()), output["name"])
                    self.prov_workflow.add_output(data_out)
                    data_out.set_producer(self.prov_workflow._id)

            for task_name in wf.steps:
                if s := wf.steps.get(task_name):
                    executions = await self.context.database.get_executions_by_step(s.persistent_id)
                    for execution_wf in executions:
                        clean_name = task_name.lstrip('/')
                        task = Task(str(uuid.uuid4()), clean_name)
                        task._start_time = streamflow.core.utils.get_date_from_ns(execution_wf["start_time"])
                        task._end_time = streamflow.core.utils.get_date_from_ns(execution_wf["end_time"])
                        task._status = self._get_action_status(Status(execution_wf["status"]))
                        task._level = '1'
                        self.prov_workflow.add_task(task)
                        
                        if clean_name not in self.tasks_by_step_name:
                            self.tasks_by_step_name[clean_name] = []
                        self.tasks_by_step_name[clean_name].append(task)

                        if task_name != clean_name:
                            if task_name not in self.tasks_by_step_name:
                                self.tasks_by_step_name[task_name] = []
                            self.tasks_by_step_name[task_name].append(task)

                        inputs = await self.context.database.get_input_ports(s.persistent_id)
                        for input_port in inputs:
                            data_in = Data(str(uuid.uuid4()), input_port["name"])
                            task.add_input(data_in)
                            data_in.add_consumer(task._id)

                        outputs = await self.context.database.get_output_ports(s.persistent_id)
                        for output_port in outputs:
                            data_out = Data(str(uuid.uuid4()), output_port["name"])
                            task.add_output(data_out)
                            data_out.set_producer(task._id)

            self.computed_cwl_deps = self._parse_cwl_for_dependencies()

            # Linking structural dependencies inside the data model
            for child_name, parents in self.computed_cwl_deps.items():
                child_tasks = self.tasks_by_step_name.get(child_name)
                if not child_tasks:
                    child_tasks = self.tasks_by_step_name.get(f"/{child_name}")

                if child_tasks:    
                    for parent_name in parents:
                        parent_tasks = self.tasks_by_step_name.get(parent_name)
                        if not parent_tasks:
                            parent_tasks = self.tasks_by_step_name.get(f"/{parent_name}")

                        if parent_tasks:
                            for p_task in parent_tasks:
                                for c_task in child_tasks:
                                    if p_task._id == c_task._id: continue
                                    if hasattr(c_task, 'set_next'):
                                        c_task.set_next(p_task)
                                    elif hasattr(c_task, 'add_next'):
                                        c_task.add_next(p_task)

            return self.prov_workflow

    async def create_archive(
        self,
        outdir: str,
        filename: Optional[str],
        config: Optional[str],
        additional_files: Optional[MutableSequence[MutableMapping[str, str]]],
        additional_properties: Optional[MutableSequence[MutableMapping[str, str]]],
    ):
        if config is not None:
            self.map_file["config"] = config
        
        self.prov_workflow = await self.populate_prov_workflow() 
                    
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, filename or (self.workflows[0].name + ".zip"))
        
        # Generate the initial JSON file
        json_file_path = self.prov_workflow.prov_to_json()  
        
        # ----------------------------------------------------------------------
        # POST-SERIALIZATION GRAPH SYNCHRONIZATION
        # ----------------------------------------------------------------------
        try:
            with open(json_file_path, 'r') as f:
                prov_data = json.load(f)
            
            if "wasInformedBy" not in prov_data:
                prov_data["wasInformedBy"] = {}

            # Audit established relations
            existing_relations = set()
            if isinstance(prov_data.get("wasInformedBy"), dict):
                for rel in prov_data["wasInformedBy"].values():
                    if isinstance(rel, dict) and "prov:informed" in rel and "prov:informant" in rel:
                        existing_relations.add((rel["prov:informed"], rel["prov:informant"]))

            injected_counter = 0

            # Step across explicit dependencies discovered from the CWL structure
            for child_path, parent_paths in self.computed_cwl_deps.items():
                # Cross-reference execution arrays using comprehensive symmetric fallbacks
                child_tasks = (self.tasks_by_step_name.get(child_path) or 
                               self.tasks_by_step_name.get(child_path.lstrip('/')) or 
                               self.tasks_by_step_name.get(f"/{child_path.lstrip('/')}"))
                if not child_tasks:
                    continue

                for parent_path in parent_paths:
                    parent_tasks = (self.tasks_by_step_name.get(parent_path) or 
                                    self.tasks_by_step_name.get(parent_path.lstrip('/')) or 
                                    self.tasks_by_step_name.get(f"/{parent_path.lstrip('/')}"))
                    if not parent_tasks:
                        continue

                    # Guarantee all active parent elements match their corresponding child definitions
                    for p_task in parent_tasks:
                        for c_task in child_tasks:
                            p_uuid = p_task._id
                            c_uuid = c_task._id

                            # Prevent circular self-references and block pre-existing definitions
                            if p_uuid != c_uuid and (c_uuid, p_uuid) not in existing_relations:
                                relation_key = f"_:informed_{uuid.uuid4().hex[:8]}"
                                prov_data["wasInformedBy"][relation_key] = {
                                    "prov:informed": c_uuid,
                                    "prov:informant": p_uuid
                                }
                                existing_relations.add((c_uuid, p_uuid))
                                injected_counter += 1

            logger.info(f"YPROV: Synchronized {injected_counter} 'wasInformedBy' edges during validation layout step.")
            
            with open(json_file_path, 'w') as f:
                json.dump(prov_data, f, indent=4)

        except Exception as e:
            logger.error(f"YPROV: Internal synchronization error occurred while creating archive: {e}")
        # ----------------------------------------------------------------------
        
        # ----------------------------------------------------------------------
        # PURGE LEVEL 0 ACTIVITY + ASSOCIATED ENTITIES
        # ----------------------------------------------------------------------
        try:
            with open(json_file_path, 'r') as f:
                prov_data = json.load(f)
            
            level_0_id = getattr(self.prov_workflow, '_id', None)
            
            if level_0_id:
                entities_to_purge = set()

                # Scan relationships to identify all entity IDs linked to level 0
                if "used" in prov_data and isinstance(prov_data["used"], dict):
                    for rel in prov_data["used"].values():
                        if isinstance(rel, dict) and rel.get("prov:activity") == level_0_id and "prov:entity" in rel:
                            entities_to_purge.add(rel["prov:entity"])
                            
                if "wasGeneratedBy" in prov_data and isinstance(prov_data["wasGeneratedBy"], dict):
                    for rel in prov_data["wasGeneratedBy"].values():
                        if isinstance(rel, dict) and rel.get("prov:activity") == level_0_id and "prov:entity" in rel:
                            entities_to_purge.add(rel["prov:entity"])

                # Delete the identified entities from the 'entity' block
                if "entity" in prov_data and isinstance(prov_data["entity"], dict):
                    for ent_id in entities_to_purge:
                        if ent_id in prov_data["entity"]:
                            del prov_data["entity"][ent_id]
                    logger.info(f"YPROV: Purged {len(entities_to_purge)} level 0 entities.")

                # Delete the level 0 activity itself
                if "activity" in prov_data and isinstance(prov_data["activity"], dict) and level_0_id in prov_data["activity"]:
                    del prov_data["activity"][level_0_id]
                    logger.info(f"YPROV: Purged level 0 activity '{level_0_id}'.")

                # Clean up all relationship edges involving the level 0 node
                for rel_type in ["wasInformedBy", "used", "wasGeneratedBy", "wasAssociatedWith"]:
                    if rel_type in prov_data and isinstance(prov_data[rel_type], dict):
                        keys_to_delete = [
                            k for k, v in prov_data[rel_type].items() 
                            if isinstance(v, dict) and (
                                v.get("prov:activity") == level_0_id or 
                                v.get("prov:informant") == level_0_id or 
                                v.get("prov:informed") == level_0_id
                            )
                        ]
                        for k in keys_to_delete:
                            del prov_data[rel_type][k]
                            
                # Write the completely sanitized structure back to JSON
                with open(json_file_path, 'w') as f:
                    json.dump(prov_data, f, indent=4)
                    
        except Exception as e:
            logger.warning(f"YPROV: Failed to execute total purge of level 0: {e}")
        # ----------------------------------------------------------------------

        # Proceed with zipping the clean JSON file
        with ZipFile(path, "w") as archive:
            archive.write(json_file_path, arcname="provenance.json")  
            for src, dst in self.map_file.items():
                if os.path.exists(src):
                    if dst not in archive.namelist():
                        archive.write(src, dst)
                else:
                    logger.warning(f"YPROV: File {src} does not exist.")
        
        print(f"YPROV: Successfully created yProv4WFs archive at {path}")