"""
This module extracts workflow provenance data directly from the StreamFlow database
after execution has finished.

Moreover, it includes:
- CWL crawler for finding all cwl files involved
- Handling multi-tier nested workflows for dependency solving.

The CWL entry point is resolved directly from the workflow's own DB record
(workflow.params.config.file) rather than from a streamflow.yml on disk,
offline extraction is only ever given a workflow ID/name, and may run from a
different directory or long after the original submission, so a hardcoded
streamflow.yml path was never reliable here.
"""

import os
import uuid
import json
import yaml
import logging
import hashlib
from abc import abstractmethod
from zipfile import ZipFile
from urllib.parse import urlparse, unquote
from typing import Any, MutableMapping, MutableSequence, Optional, List, Tuple

import streamflow.core.utils
from streamflow.core.provenance import ProvenanceManager
from streamflow.core.workflow import Status, Workflow as StreamFlowWorkflow
from streamflow.core.context import StreamFlowContext
from streamflow.core.persistence import DatabaseLoadingContext
from streamflow.log_handler import logger

from yprov4wfs.datamodel.workflow import Workflow
from yprov4wfs.datamodel.task import Task
from yprov4wfs.datamodel.data import Data

# Silence aiosqlite background logging
logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def _crawl_cwl_dependencies(main_cwl_path: str) -> list[str]:
    """
    Recursively crawls a KNOWN CWL entry-point file for every 'run: ...cwl'
    reference it (transitively) contains. Doesn't care how main_cwl_path was
    determined, that's the job of the two resolver functions below.
    """
    if not main_cwl_path or not os.path.exists(main_cwl_path):
        logger.warning(f"YPROV [DISCOVERY]: Unable to locate primary CWL file at: {main_cwl_path}")
        return []

    logger.info(f"YPROV [DISCOVERY]: Starting CWL discovery crawl from entrypoint: {main_cwl_path}")

    to_parse = [os.path.realpath(main_cwl_path)]
    discovered_files = set(to_parse)

    def extract_run_paths(data):
        """Deep crawler that finds any 'run' key with a .cwl file string."""
        paths = []
        if isinstance(data, dict):
            for k, v in data.items():
                if k == 'run' and isinstance(v, str) and v.endswith('.cwl'):
                    paths.append(v)
                else:
                    paths.extend(extract_run_paths(v))
        elif isinstance(data, list):
            for item in data:
                paths.extend(extract_run_paths(item))
        return paths

    while to_parse:
        current_file = to_parse.pop(0)
        base_dir = os.path.dirname(current_file)

        try:
            with open(current_file, 'r') as f:
                content = yaml.safe_load(f) or {}

            # Find all external files referenced anywhere in this document
            relative_paths = extract_run_paths(content)

            for rel_path in relative_paths:
                # Strip out any URI encoding if present
                clean_path = unquote(urlparse(rel_path).path)
                full_path = os.path.realpath(os.path.join(base_dir, clean_path))

                if full_path not in discovered_files and os.path.exists(full_path):
                    discovered_files.add(full_path)
                    to_parse.append(full_path)
                    logger.info(f"YPROV [DISCOVERY]: Discovered nested CWL file: {full_path}")

        except Exception as e:
            logger.warning(f"YPROV [DISCOVERY]: Could not deep-parse {current_file}: {e}")

    logger.info(f"YPROV [DISCOVERY]: Total CWL workflow files discovered: {len(discovered_files)}")
    return list(discovered_files)


def _resolve_main_cwl_from_db_params(raw_params: Any, base_dir: str) -> Optional[str]:
    """
    Resolves the CWL entry point directly from workflow.params, e.g.:
    {"config": {"file": "./main.cwl", "settings": "./inputs.yml"}, ...}

    This is the ONLY resolution path for offline extraction: unlike the
    online plugin (which runs inside the same process StreamFlow was
    invoked from, and can inspect sys.argv/cwd at that moment), offline
    extraction runs later, possibly from a different working directory,
    possibly long after the run, so there was never a reliable way to find
    a streamflow.yml on disk for it to parse in the first place.
    workflow.params is the authoritative record of what CWL file this
    SPECIFIC execution actually used, persisted at submission time
    regardless of what's on disk now.

    base_dir anchors the relative path from params (e.g. "./main.cwl")
    there's no absolute base directory recorded in params itself, so this is
    always the current working directory at the time extraction is run.
    """
    try:
        main_cwl_relative = json.loads(raw_params, strict=False)["config"]["file"]
    except Exception as e:
        logger.warning(f"YPROV [DB_EXTRACT]: Could not read config.file from workflow.params: {e}")
        return None

    if not main_cwl_relative:
        logger.warning("YPROV [DB_EXTRACT]: workflow.params has no config.file entry.")
        return None

    resolved = os.path.abspath(os.path.realpath(os.path.join(base_dir, main_cwl_relative)))
    logger.info(f"YPROV [DB_EXTRACT]: Resolved CWL entry point from DB params: {resolved} (relative to {base_dir})")
    return resolved


def discover_workflow_cwl_files(main_cwl_path: Optional[str]) -> list[str]:
    """
    Finds every CWL file (transitively) referenced from a workflow's entry
    point, given the entry point already resolved from workflow.params (see
    _resolve_main_cwl_from_db_params).
    """
    if not main_cwl_path or not os.path.exists(main_cwl_path):
        logger.warning(f"YPROV [DISCOVERY]: Unable to locate primary CWL file at: {main_cwl_path}")
        return []

    logger.info(f"YPROV [DISCOVERY]: Using CWL entry point: {main_cwl_path}")
    return _crawl_cwl_dependencies(main_cwl_path)


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
        self.main_cwl_path: Optional[str] = None
        
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
        cwl_files = discover_workflow_cwl_files(self.main_cwl_path)

        if not cwl_files:
            logger.warning("YPROV [PARSER]: No active CWL files discovered via graph parsing.")
            return dependencies

        logger.info(f"YPROV [PARSER]: Files selected for analysis: {cwl_files}")

        # Build an index of loaded CWL contents by their filename
        cwl_registry = {}
        for filename in cwl_files:
            try:
                real_filename = os.path.abspath(os.path.realpath(filename))
                with open(real_filename, 'r') as f:
                    data = yaml.safe_load(f)
                    if data:
                        cwl_registry[os.path.basename(filename)] = data
            except Exception as e:
                logger.warning(f"YPROV [PARSER]: Error reading file {filename}: {e}")
                continue

        # Build a set of valid execution paths from the runtime map 
        # (Prevents duplicate short names from overwriting each other)
        valid_absolute_paths = set()
        for full_path in self.tasks_by_step_name.keys():
            normalized_path = '/' + full_path.lstrip('/')
            valid_absolute_paths.add(normalized_path)

        def extract_steps_recursive(workflow_data, current_prefix=""):
            if not isinstance(workflow_data, dict) or workflow_data.get('class') != 'Workflow':
                return
            
            steps = workflow_data.get('steps', {})
            steps_items = steps.items() if isinstance(steps, dict) else [(s['id'], s) for s in steps]

            sibling_shorts = [step_id.split('/')[-1] for step_id, _ in steps_items]

            for step_id, step_val in steps_items:
                short_step_name = step_id.split('/')[-1]
                full_step_name = f"{current_prefix}/{short_step_name}"

                # Unconditionally add every step found in the CWL
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
                                parent_short_name = s.split('/')[0].split('#')[-1]
                                
                                if parent_short_name not in sibling_shorts and current_prefix:
                                    prefix_parts = current_prefix.lstrip('/').split('/')
                                    if len(prefix_parts) > 1:
                                        parent_env = "/" + "/".join(prefix_parts[:-1])
                                        full_parent_name = f"{parent_env}/{parent_short_name}"
                                    else:
                                        full_parent_name = f"/{parent_short_name}"
                                else:
                                    full_parent_name = f"{current_prefix}/{parent_short_name}"

                                # Unconditionally map the parent relationship
                                if full_parent_name not in dependencies[full_step_name]:
                                    dependencies[full_step_name].append(full_parent_name)

                # Recursive check
                if isinstance(step_val, dict) and 'run' in step_val:
                    run_target = step_val['run']
                    next_prefix = f"{current_prefix}/{short_step_name}"
                    
                    if isinstance(run_target, dict):
                        extract_steps_recursive(run_target, current_prefix=next_prefix)
                    elif isinstance(run_target, str):
                        target_filename = os.path.basename(run_target)
                        if target_filename in cwl_registry:
                            extract_steps_recursive(cwl_registry[target_filename], current_prefix=next_prefix)
                            
        # Locate the main root workflow: the entry point resolved from
        # workflow.params in populate_prov_workflow(), the authoritative
        # record of what THIS execution actually used. Falls back to "any
        # file that declares itself class: Workflow" only if DB resolution
        # somehow came back empty.
        main_workflow_file = None

        if self.main_cwl_path:
            main_workflow_file = os.path.basename(self.main_cwl_path)
            logger.info(f"YPROV [PARSER]: Using DB-resolved root workflow entry point: {main_workflow_file}")

        if not main_workflow_file or main_workflow_file not in cwl_registry:
            for base_name, data in cwl_registry.items():
                if isinstance(data, dict) and data.get('class') == 'Workflow':
                    main_workflow_file = base_name
                    break

        # Kick off parsing
        if main_workflow_file:
            logger.info(f"YPROV [PARSER]: Starting hierarchical parsing from entrypoint: {main_workflow_file}")
            extract_steps_recursive(cwl_registry[main_workflow_file], current_prefix="")
        else:
            logger.warning("YPROV [PARSER]: Failed to locate a primary master Workflow file to analyze.")

        logger.info(f"YPROV [PARSER]: Computed task dependency links: {dependencies}")
        return dependencies
    
    async def populate_prov_workflow(self) -> Workflow:
        """
        Reads the executed tasks and ports from the database.
        Applies granular execution tracking for scatter nodes while 
        deduplicating their shared array entities.
        """
        logger.info("YPROV [DB_EXTRACT]: Beginning provenance extraction from database...")
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

            # Resolve the CWL entry point directly from the DB record, since
            # offline extraction never has a streamflow.yml path handed to it
            # the way the online plugin does -- only the workflow ID/name.
            base_dir = os.getcwd()
            self.main_cwl_path = _resolve_main_cwl_from_db_params(wf_obj["params"], base_dir)
            if self.main_cwl_path:
                self.prov_workflow._resource_cwl_uri = self.main_cwl_path
            else:
                logger.warning(
                    "YPROV [DB_EXTRACT]: Could not resolve a CWL entry point from workflow.params -- "
                    "CWL dependency parsing will be skipped for this workflow."
                )

            # Extract tasks and scatter iterations
            task_count = 0
            for task_name in wf.steps:
                clean_name = task_name.lstrip('/')
                
                if s := wf.steps.get(task_name):
                    executions = await self.context.database.get_executions_by_step(s.persistent_id)
                    
                    for execution_wf in executions:
                        task_count += 1
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

            logger.info(f"YPROV [DB_EXTRACT]: Successfully parsed {task_count} task execution records from DB.")

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
            logger.info("YPROV [ARCHIVE]: Reconciling graph edges into PROV-JSON schema...")
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

            def _resolve_leaf_tasks(step_name: str) -> List[Task]:
                """Resolves a CWL step to its leaf tasks via direct or prefix match."""
                exact_match = self.tasks_by_step_name.get(step_name) or self.tasks_by_step_name.get(f"/{step_name.lstrip('/')}")
                if exact_match:
                    return exact_match
                
                prefix = f"/{step_name.lstrip('/')}/"
                child_tasks = []
                for task_name, tasks in self.tasks_by_step_name.items():
                    normalized_name = f"/{task_name.lstrip('/')}"
                    if normalized_name.startswith(prefix):
                        child_tasks.extend(tasks)
                return child_tasks

            for child_path, parent_paths in self.computed_cwl_deps.items():
                child_tasks = _resolve_leaf_tasks(child_path)
                if not child_tasks: 
                    continue

                for parent_path in parent_paths:
                    parent_tasks = _resolve_leaf_tasks(parent_path)
                    if not parent_tasks: 
                        continue

                    p_len, c_len = len(parent_tasks), len(child_tasks)
                    
                    if p_len == 1 and c_len > 1:
                        for c_task in child_tasks: 
                            _inject_edge(parent_tasks[0]._id, c_task._id)
                    elif c_len == 1 and p_len > 1:
                        for p_task in parent_tasks: 
                            _inject_edge(p_task._id, child_tasks[0]._id)
                    elif p_len == c_len:
                        for p_task, c_task in zip(parent_tasks, child_tasks): 
                            _inject_edge(p_task._id, c_task._id)
                    else:
                        for p_task in parent_tasks:
                            for c_task in child_tasks: 
                                _inject_edge(p_task._id, c_task._id)

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

            with open(json_file_path, 'w') as f: 
                json.dump(prov_data, f, indent=4)

            # Log provenance json file size metrics
            json_bytes = os.path.getsize(json_file_path)
            logger.info(f"YPROV [ARCHIVE]: [PROV_FILE_SIZE] provenance.json = {json_bytes} Bytes")

        except Exception as e:
            logger.error(f"YPROV [ARCHIVE]: Internal sync failure during JSON edge cleanup: {e}")

        with ZipFile(path, "w") as archive:
            archive.write(json_file_path, arcname="provenance.json")  
            for src, dst in self.map_file.items():
                if os.path.exists(src) and dst not in archive.namelist(): 
                    archive.write(src, dst)

        zip_bytes = os.path.getsize(path)
        logger.info(f"YPROV [ARCHIVE]: [PROV_ARCHIVE_SIZE] {os.path.basename(path)} = {zip_bytes} Bytes")
        logger.info(f"YPROV [ARCHIVE]: Successfully generated offline yProv4WFs archive at {path}")