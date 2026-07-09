"""
This module extracts workflow provenance data directly from the StreamFlow database
after execution has finished.

Moreover, it includes:
- Path normalization for handling multi-tier nested workflows.
- Complete removal of the Level 0 activity node and its peripheral entities.
"""

import os
import uuid
import json
import yaml
import logging
import hashlib
from abc import abstractmethod
from zipfile import ZipFile
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import Any, MutableMapping, MutableSequence, Optional, Set, List, Tuple

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

# Silence aiosqlite background logging
logging.getLogger("aiosqlite").setLevel(logging.WARNING)

def discover_workflow_cwl_files(streamflow_config_path: Optional[str]) -> list[str]:
    """
    Parses the primary streamflow.yml deployment config file to isolate the entrypoint,
    and uses cwl-utils to recursively trace and canonicalize all external CWL files.
    """
    main_cwl = None
    if streamflow_config_path and os.path.exists(streamflow_config_path):
        try:
            # Resolve symbolic links on the configuration directory right away
            real_config_path = os.path.abspath(os.path.realpath(streamflow_config_path))
            with open(real_config_path, 'r') as sf_file:
                sf_data = yaml.safe_load(sf_file)
            
            workflows = sf_data.get("workflows", {})
            if workflows and isinstance(workflows, dict):
                first_workflow_name = next(iter(workflows))
                workflow_data = workflows.get(first_workflow_name, {})
                main_cwl_relative = workflow_data.get("config", {}).get("file")
                
                if main_cwl_relative:
                    config_dir = os.path.dirname(real_config_path)
                    main_cwl = os.path.abspath(os.path.realpath(os.path.join(config_dir, main_cwl_relative)))
        except Exception as e:
            logger.warning(f"YPROV: Error parsing streamflow.yml ({streamflow_config_path}): {e}")

    if not main_cwl or not os.path.exists(main_cwl):
        logger.warning(f"YPROV: Unable to determine a valid primary CWL file. Resolved: {main_cwl}")
        return []

    cwl_files_paths: Set[str] = set()

    def discover_recursive(file_path: str):
        # Resolve symlink to the absolute physical target path
        abs_path = os.path.abspath(os.path.realpath(file_path))
        if abs_path in cwl_files_paths:
            return
        cwl_files_paths.add(abs_path)
        
        try:
            uri = Path(abs_path).resolve().as_uri()
            doc = load_document_by_uri(uri)
            process_list = doc if isinstance(doc, list) else [doc]
            
            for process in process_list:
                # If this document contains steps (is a workflow)
                if hasattr(process, 'steps') and process.steps:
                    for step in process.steps:
                        if hasattr(step, 'run'):
                            # Case A: Step runs an external file string
                            if isinstance(step.run, str):
                                parsed = urlparse(step.run)
                                if parsed.scheme in ('file', ''):
                                    next_file = unquote(parsed.path)
                                    if not os.path.isabs(next_file):
                                        next_file = os.path.join(os.path.dirname(abs_path), next_file)
                                    if os.path.exists(next_file):
                                        discover_recursive(next_file)
                            
                            # Case B: Embedded Inline sub-workflow object (cwl-utils automatically unpacked it)
                            elif hasattr(step.run, 'steps') and step.run.steps:
                                # Trigger discovery on the main file again to ensure its internal paths are evaluated
                                discover_recursive(abs_path)
        except Exception as e:
            logger.warning(f"YPROV: Error parsing graph references for {file_path}: {e}")

    discover_recursive(main_cwl)
    return list(cwl_files_paths)


class yProv4WFsProvenanceManager(ProvenanceManager):
    """
    Offline Provenance Manager for StreamFlow databases.
    Handles the reconstruction of the workflow execution graph, mapping CWL 
    definitions to their respective execution records.
    """
    
    def __init__(self, context: StreamFlowContext, db_context: DatabaseLoadingContext, workflows: MutableSequence[StreamFlowWorkflow]):
        super().__init__(context, db_context, workflows)
        self.map_file: MutableMapping[str, str] = {}
        self.prov_workflow = None
        
        # Tracking dictionaries for dependency resolution
        self.tasks_by_step_name: MutableMapping[str, List[Task]] = {}
        self.computed_cwl_deps: MutableMapping[str, List[str]] = {}
        
    @abstractmethod
    async def get_main_entity(self) -> MutableMapping[str, Any]: ...
    
    @abstractmethod
    async def add_initial_inputs(self, wf_id: int, workflow: Workflow) -> None: ...

    def _get_action_status(self, status: Status) -> str:
        """Translates StreamFlow status enums to standard PROV strings."""
        if status == Status.COMPLETED: return "Completed"
        elif status == Status.FAILED: return "Failed"
        elif status in [Status.CANCELLED, Status.SKIPPED]: return "Cancelled or Skipped"
        return "Running"

    def _extract_port_metadata(self, port_db_record: Any) -> Tuple[str, str, str]:
        """
        Reconstructs type and location signatures from database port value blobs.
        
        Args:
            port_db_record (Any): The database dictionary entry for a port.
            
        Returns:
            Tuple[str, str, str]: Extracted (type, value, location).
        """
        p_type = "string"
        p_val = "None"
        p_loc = "None"
        try:
            val = port_db_record.get("value")
            if isinstance(val, str) and (val.startswith("{") or val.startswith("[")):
                try: 
                    val = json.loads(val)
                except Exception: 
                    pass
                    
            if isinstance(val, dict):
                p_type = val.get("class", "File" if "path" in val or "location" in val else "string")
                p_loc = val.get("location") or val.get("path") or "None"
                p_val = os.path.basename(p_loc) if p_loc != "None" else str(val)
            elif isinstance(val, list):
                p_type = "array"
                p_val = str([v.get("location") if isinstance(v, dict) else str(v) for v in val])
            elif val is not None:
                p_type = type(val).__name__
                p_val = str(val)
        except Exception: 
            pass
            
        return p_type, p_val, p_loc

    def _parse_cwl_for_dependencies(self) -> MutableMapping[str, List[str]]:
        """
        Inspects structural CWL workflow graphs recursively to deduce correct task 
        input-to-output dependencies, fully supporting inline and nested steps.
        """
        dependencies = {}
        streamflow_config_path = self.map_file.get("config")
        cwl_files = discover_workflow_cwl_files(streamflow_config_path)

        if not cwl_files:
            logger.warning("YPROV: No active CWL files discovered via graph parsing.")
            return dependencies

        logger.info(f"YPROV: Normalized files selected for analysis: {cwl_files}")

        # Rebuild full path map, enforcing normalized canonical naming paths
        full_path_map = {}
        for full_path in self.tasks_by_step_name.keys():
            normalized_path = '/' + full_path.lstrip('/')
            short_name = normalized_path.split('/')[-1]
            full_path_map[short_name] = normalized_path

        def extract_steps_recursive(workflow_data, current_prefix=""):
            if not isinstance(workflow_data, dict) or workflow_data.get('class') != 'Workflow':
                return
            
            steps = workflow_data.get('steps', {})
            steps_items = steps.items() if isinstance(steps, dict) else [(s['id'], s) for s in steps]

            for step_id, step_val in steps_items:
                short_step_name = step_id.split('/')[-1]
                
                # Formulate hierarchical matching paths for nested structures
                lookup_name = f"{current_prefix}/{short_step_name}" if current_prefix else short_step_name
                full_step_name = full_path_map.get(short_step_name) or full_path_map.get(lookup_name.lstrip('/'))

                if full_step_name:
                    if full_step_name not in dependencies:
                        dependencies[full_step_name] = []
                    
                    inputs = step_val.get('in', [])
                    input_list = inputs if isinstance(inputs, list) else [{'source': v} for v in inputs.values()]

                    for inp in input_list:
                        src = inp.get('source') if isinstance(inp, dict) else inp
                        if src:
                            sources = src if isinstance(src, list) else [src]
                            for s in sources:
                                if '/' in s:
                                    parent_short_name = s.split('/')[0]
                                    parent_lookup = f"{current_prefix}/{parent_short_name}" if current_prefix else parent_short_name
                                    full_parent_name = full_path_map.get(parent_short_name) or full_path_map.get(parent_lookup.lstrip('/'))
                                    if full_parent_name and full_parent_name in full_path_map.values() and full_parent_name not in dependencies[full_step_name]:
                                        dependencies[full_step_name].append(full_parent_name)

                # RECURSIVE CHECK: If step contains an inline nested workflow definition
                if isinstance(step_val, dict) and 'run' in step_val and isinstance(step_val['run'], dict):
                    next_prefix = f"{current_prefix}/{short_step_name}" if current_prefix else short_step_name
                    extract_steps_recursive(step_val['run'], current_prefix=next_prefix)

        for filename in cwl_files:
            try:
                # Enforce symlink resolution during direct YAML reading
                real_filename = os.path.abspath(os.path.realpath(filename))
                with open(real_filename, 'r') as f:
                    data = yaml.safe_load(f)
                
                extract_steps_recursive(data)
            except Exception as e:
                logger.warning(f"YPROV: Error recursively mapping file {filename}: {e}")
                continue

        logger.info(f"YPROV Dependencies list computed: {dependencies}")
        return dependencies
    
    async def populate_prov_workflow(self) -> Workflow:
        """
        Reads the executed tasks and ports from the database.
        Applies granular execution tracking for scatter nodes while 
        deduplicating their shared array entities.
        """
        self.tasks_by_step_name = {}
        
        for wf in self.workflows:
            wf_obj = await self.context.database.get_workflow(wf.persistent_id)
            
            # Initialize workflow metadata
            self.prov_workflow = Workflow(wf_obj["name"], f'workflow_{wf_obj["name"]}')
            self.prov_workflow._start_time = streamflow.core.utils.get_date_from_ns(wf_obj["start_time"])
            self.prov_workflow._end_time = streamflow.core.utils.get_date_from_ns(wf_obj["end_time"])
            self.prov_workflow._status = self._get_action_status(Status(wf_obj["status"]))
            self.prov_workflow._engineWMS = 'StreamFlow'
            self.prov_workflow._level = '0'
            
            if "config" in self.map_file: 
                self.prov_workflow._resource_cwl_uri = self.map_file["config"]

            # Extract tasks and scatter iterations
            for task_name in wf.steps:
                clean_name = task_name.lstrip('/')
                
                if s := wf.steps.get(task_name):
                    executions = await self.context.database.get_executions_by_step(s.persistent_id)
                    
                    for execution_wf in executions:
                        task = Task(str(uuid.uuid4()), clean_name)
                        task._start_time = streamflow.core.utils.get_date_from_ns(execution_wf["start_time"])
                        task._end_time = streamflow.core.utils.get_date_from_ns(execution_wf["end_time"])
                        task._status = self._get_action_status(Status(execution_wf["status"]))
                        task._level = '1'
                        
                        self.prov_workflow.add_task(task)
                        
                        # Register task mappings for topological dependency resolution
                        if clean_name not in self.tasks_by_step_name: 
                            self.tasks_by_step_name[clean_name] = []
                        self.tasks_by_step_name[clean_name].append(task)
                        
                        if task_name != clean_name:
                            if task_name not in self.tasks_by_step_name: 
                                self.tasks_by_step_name[task_name] = []
                            self.tasks_by_step_name[task_name].append(task)

                        # Extract input ports
                        inputs = await self.context.database.get_input_ports(s.persistent_id)
                        for input_port in inputs:
                            port_label = input_port["name"].split('/')[-1]
                            label_low = port_label.lower()
                            
                            # Drop Streamflow internal control tokens
                            if ("__" in port_label or port_label.startswith("_") or "job" in label_low or 
                                "-injector" in label_low or "-collector" in label_low or "token" in label_low): 
                                continue

                            # Use a deterministic hash of the step name and port label to avoid entity duplication
                            data_id = f"ent_in_{hashlib.md5(f'{clean_name}_{port_label}'.encode()).hexdigest()[:8]}"
                            data_in = Data(data_id, port_label)
                            dt, dv, dl = self._extract_port_metadata(input_port)
                            data_in._type, data_in._value, data_in._location = dt, dv, dl
                            
                            task.add_input(data_in)
                            data_in.add_consumer(task._id)

                        # Extract output ports
                        outputs = await self.context.database.get_output_ports(s.persistent_id)
                        for output_port in outputs:
                            port_label = output_port["name"].split('/')[-1]
                            label_low = port_label.lower()
                            
                            if ("__" in port_label or port_label.startswith("_") or "job" in label_low or 
                                "-injector" in label_low or "-collector" in label_low or "token" in label_low): 
                                continue
                            
                            # Use a deterministic hash of the step name and port label to avoid entity duplication
                            data_id = f"ent_out_{hashlib.md5(f'{clean_name}_{port_label}'.encode()).hexdigest()[:8]}"
                            data_out = Data(data_id, port_label)
                            dt, dv, dl = self._extract_port_metadata(output_port)
                            data_out._type, data_out._value, data_out._location = dt, dv, dl
                            
                            task.add_output(data_out)
                            data_out.set_producer(task._id)

            # Resolve structural dependencies
            self.computed_cwl_deps = self._parse_cwl_for_dependencies()
            return self.prov_workflow

    async def create_archive(self, outdir: str, filename: Optional[str], config: Optional[str], additional_files: Optional[MutableSequence[MutableMapping[str, str]]], additional_properties: Optional[MutableSequence[MutableMapping[str, str]]]):
        """
        Generates the PROV-JSON representation, performs structural edge reconciliation, 
        and packages the results into a ZIP archive.
        """
        if config is not None: 
            self.map_file["config"] = config
            
        self.prov_workflow = await self.populate_prov_workflow() 
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, filename or (self.workflows[0].name + ".zip"))
        json_file_path = self.prov_workflow.prov_to_json()  
        
        try:
            with open(json_file_path, 'r') as f: 
                prov_data = json.load(f)
                
            prov_data["wasInformedBy"] = {} 
            existing_relations = set()

            def _inject_edge(p_uuid: str, c_uuid: str) -> None:
                """Helper to inject cryptographically stable dependency edges."""
                if p_uuid != c_uuid and (c_uuid, p_uuid) not in existing_relations:
                    rel_key = f"_:informed_{hashlib.md5(f'{c_uuid}{p_uuid}'.encode()).hexdigest()[:8]}"
                    prov_data["wasInformedBy"][rel_key] = {"prov:informed": c_uuid, "prov:informant": p_uuid}
                    existing_relations.add((c_uuid, p_uuid))

            for child_path, parent_paths in self.computed_cwl_deps.items():
                child_tasks = self.tasks_by_step_name.get(child_path) or self.tasks_by_step_name.get(f"/{child_path.lstrip('/')}")
                if not child_tasks: 
                    continue

                for parent_path in parent_paths:
                    parent_tasks = self.tasks_by_step_name.get(parent_path) or self.tasks_by_step_name.get(f"/{parent_path.lstrip('/')}")
                    if not parent_tasks: 
                        continue

                    p_len, c_len = len(parent_tasks), len(child_tasks)
                    
                    if p_len == 1 and c_len > 1:
                        # One parent informs many children (e.g., standard step to scatter)
                        for c_task in child_tasks: 
                            _inject_edge(parent_tasks[0]._id, c_task._id)
                    elif c_len == 1 and p_len > 1:
                        # Many parents inform one child (e.g., scatter gather)
                        for p_task in parent_tasks: 
                            _inject_edge(p_task._id, child_tasks[0]._id)
                    elif p_len == c_len:
                        # Parallel arrays: map 1-to-1 topologically
                        for p_task, c_task in zip(parent_tasks, child_tasks): 
                            _inject_edge(p_task._id, c_task._id)
                    else:
                        # Fallback brute-force map if iteration counts differ unexpectedly
                        for p_task in parent_tasks:
                            for c_task in child_tasks: 
                                _inject_edge(p_task._id, c_task._id)

            # Purges any internal engine structural activities (noise) that bled into the graph
            purged_ids = set()
            def check_spur(s: str) -> bool: 
                return "__" in s or "job" in s.lower() or "-injector" in s.lower() or "-collector" in s.lower() or "-token-transformer" in s.lower() or "-scatter" in s.lower() or "-condition" in s.lower()
            
            if "activity" in prov_data:
                for act_id, act_meta in list(prov_data["activity"].items()):
                    if check_spur(act_meta.get("prov:label", "")) or check_spur(act_id):
                        purged_ids.add(act_id)
                        del prov_data["activity"][act_id]
                        
            if "entity" in prov_data:
                for ent_id, ent_meta in list(prov_data["entity"].items()):
                    if check_spur(ent_meta.get("prov:label", "")) or check_spur(ent_id):
                        purged_ids.add(ent_id)
                        del prov_data["entity"][ent_id]
                        
            level_0_id = getattr(self.prov_workflow, '_id', None)
            if level_0_id: 
                purged_ids.add(level_0_id)
                prov_data.get("activity", {}).pop(level_0_id, None)
                
            for r_type in ["wasInformedBy", "used", "wasGeneratedBy", "wasAssociatedWith"]:
                if r_type in prov_data:
                    for k in [k for k, v in prov_data[r_type].items() if any(v.get(p) in purged_ids for p in ["prov:activity", "prov:informant", "prov:informed", "prov:entity"])]:
                        del prov_data[r_type][k]

            # Write final aligned JSON
            with open(json_file_path, 'w') as f: 
                json.dump(prov_data, f, indent=4)
                
        except Exception as e:
            logger.error(f"YPROV: Internal sync failure: {e}")

        with ZipFile(path, "w") as archive:
            archive.write(json_file_path, arcname="provenance.json")  
            for src, dst in self.map_file.items():
                if os.path.exists(src) and dst not in archive.namelist(): 
                    archive.write(src, dst)
                    
        logger.info(f"YPROV: Successfully created aligned offline yProv4WFs archive at {path}")