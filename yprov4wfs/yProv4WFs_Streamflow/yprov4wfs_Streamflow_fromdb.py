"""
This module extracts workflow provenance data directly from the StreamFlow database
after execution has finished.

Moreover, it includes:
- CWL crawler for finding all cwl files involved
- Handling multi-tier nested workflows for dependency solving.

The CWL entry point is resolved directly from the workflow's own DB record
(workflow.params.config.file) rather than from a streamflow.yml on disk --
offline extraction is only ever given a workflow ID/name, and may run from a
different directory or long after the original submission, so a hardcoded
streamflow.yml path was never reliable here.
"""

import os
import uuid
import json
import yaml
import time
import logging
import hashlib
import traceback
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

def _tags_are_compatible(parent_tag: str, child_tag: str) -> bool:
    """Check if task tags match exactly or share a parent-child iteration prefix."""
    if not parent_tag or not child_tag:
        return True
    if parent_tag == child_tag:
        return True

    p_parts = str(parent_tag).strip(".").split(".")
    c_parts = str(child_tag).strip(".").split(".")
    min_len = min(len(p_parts), len(c_parts))

    return p_parts[:min_len] == c_parts[:min_len]

def _crawl_cwl_dependencies(main_cwl_path: str) -> list[str]:
    """
    Recursively crawls a KNOWN CWL entry-point file for every 'run: ...cwl'
    reference it (transitively) contains.
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

            relative_paths = extract_run_paths(content)

            for rel_path in relative_paths:
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
    Resolves the CWL entry point directly from workflow.params.
    """
    main_cwl_relative = None

    try:
        if isinstance(raw_params, dict):
            params_dict = raw_params
        elif isinstance(raw_params, (str, bytes, bytearray)):
            params_dict = json.loads(raw_params, strict=False)
        else:
            params_dict = {}

        main_cwl_relative = params_dict.get("config", {}).get("file")
    except Exception as e:
        logger.warning(f"YPROV [DB_EXTRACT]: Could not read config.file from workflow.params: {e}")

    if main_cwl_relative:
        resolved = os.path.abspath(os.path.realpath(os.path.join(base_dir, main_cwl_relative)))
        if os.path.exists(resolved):
            logger.info(f"YPROV [DB_EXTRACT]: Resolved CWL entry point from DB params: {resolved}")
            return resolved
        else:
            logger.warning(f"YPROV [DB_EXTRACT]: DB params point to missing file: {resolved}. Triggering streamflow.yml fallback...")

    # Fallback to streamflow.yml
    streamflow_yml_path = os.path.join(base_dir, "streamflow.yml")
    if os.path.exists(streamflow_yml_path):
        logger.info(f"YPROV [DB_EXTRACT]: Fallback engaged. Inspecting streamflow.yml at: {streamflow_yml_path}")
        try:
            with open(streamflow_yml_path, "r") as f:
                sf_config = yaml.safe_load(f) or {}

            fallback_file = None
            if "config" in sf_config and isinstance(sf_config["config"], dict):
                fallback_file = sf_config["config"].get("file")
            elif "workflows" in sf_config and isinstance(sf_config["workflows"], dict):
                for wf_k, wf_v in sf_config["workflows"].items():
                    if isinstance(wf_v, dict) and "config" in wf_v:
                        fallback_file = wf_v["config"].get("file")
                        if fallback_file:
                            break

            if fallback_file:
                resolved_fallback = os.path.abspath(os.path.realpath(os.path.join(base_dir, fallback_file)))
                if os.path.exists(resolved_fallback):
                    logger.info(f"YPROV [DB_EXTRACT]: Resolved CWL entry point from streamflow.yml: {resolved_fallback}")
                    return resolved_fallback
                else:
                    logger.warning(f"YPROV [DB_EXTRACT]: streamflow.yml points to missing file: {resolved_fallback}")
        except Exception as e:
            logger.warning(f"YPROV [DB_EXTRACT]: Failed to parse streamflow.yml: {e}")

    logger.warning("YPROV [DB_EXTRACT]: Could not resolve a valid CWL entry point from DB params or streamflow.yml.")
    return None


def discover_workflow_cwl_files(main_cwl_path: Optional[str]) -> list[str]:
    """
    Finds every CWL file (transitively) referenced from a workflow's entry point.
    """
    if not main_cwl_path or not os.path.exists(main_cwl_path):
        logger.warning(f"YPROV [DISCOVERY]: Unable to locate primary CWL file at: {main_cwl_path}")
        return []

    logger.info(f"YPROV [DISCOVERY]: Using CWL entry point: {main_cwl_path}")
    return _crawl_cwl_dependencies(main_cwl_path)


def _get_scatter_tag(task: Any) -> Optional[Tuple[int, ...]]:
    """
    Extracts the FULL hierarchical scatter tag from a task's real job name/tag,
    as a tuple of ints -- e.g. '/band/mProject/0.1.0' -> (0, 1, 0).
    """
    job_name = str(getattr(task, '_job_name', '') or '')
    if not job_name:
        return None
    tag_str = job_name.rstrip('/').split('/')[-1]
    parts = tag_str.split('.')
    if not parts or not all(p.lstrip('-').isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)

def _tags_share_lineage(p_tag: Tuple[int, ...], c_tag: Tuple[int, ...]) -> bool:
    """True iff one tag is a prefix of the other (same band/tile lineage)."""
    shorter, longer = (p_tag, c_tag) if len(p_tag) <= len(c_tag) else (c_tag, p_tag)
    return longer[:len(shorter)] == shorter


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

        cwl_registry = {}
        for filename in cwl_files:
            try:
                real_filename = os.path.abspath(os.path.realpath(filename))
                with open(real_filename, 'r') as f:
                    data = yaml.safe_load(f)
                    if data:
                        cwl_registry[real_filename] = data
            except Exception as e:
                logger.warning(f"YPROV [PARSER]: Error reading file {filename}: {e}")
                continue

        valid_absolute_paths = set()
        for full_path in self.tasks_by_step_name.keys():
            normalized_path = '/' + full_path.lstrip('/')
            valid_absolute_paths.add(normalized_path)

        def extract_steps_recursive(workflow_data, current_file_path, current_prefix=""):
            if not isinstance(workflow_data, dict) or workflow_data.get('class') != 'Workflow':
                return
            
            steps = workflow_data.get('steps', {})
            steps_items = steps.items() if isinstance(steps, dict) else [(s['id'], s) for s in steps]

            sibling_shorts = [step_id.split('/')[-1] for step_id, _ in steps_items]

            for step_id, step_val in steps_items:
                short_step_name = step_id.split('/')[-1]
                full_step_name = f"{current_prefix}/{short_step_name}"

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

                                if full_parent_name not in dependencies[full_step_name]:
                                    dependencies[full_step_name].append(full_parent_name)

                if isinstance(step_val, dict) and 'run' in step_val:
                    run_target = step_val['run']
                    next_prefix = f"{current_prefix}/{short_step_name}"
                    
                    if isinstance(run_target, dict):
                        extract_steps_recursive(run_target, current_file_path, current_prefix=next_prefix)
                    elif isinstance(run_target, str):
                        clean_run_path = unquote(urlparse(run_target).path)
                        target_filepath = os.path.abspath(os.path.realpath(os.path.join(os.path.dirname(current_file_path), clean_run_path)))
                        
                        if target_filepath in cwl_registry:
                            extract_steps_recursive(cwl_registry[target_filepath], target_filepath, current_prefix=next_prefix)

        main_workflow_file = None

        if self.main_cwl_path:
            main_workflow_file = os.path.abspath(os.path.realpath(self.main_cwl_path))
            logger.info(f"YPROV [PARSER]: Using DB-resolved root workflow entry point: {main_workflow_file}")

        if not main_workflow_file or main_workflow_file not in cwl_registry:
            for abs_path, data in cwl_registry.items():
                if isinstance(data, dict) and data.get('class') == 'Workflow':
                    main_workflow_file = abs_path
                    break

        if main_workflow_file:
            logger.info(f"YPROV [PARSER]: Starting hierarchical parsing from entrypoint: {main_workflow_file}")
            extract_steps_recursive(cwl_registry[main_workflow_file], main_workflow_file, current_prefix="")
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
        t0 = time.perf_counter()
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

                        if not hasattr(self, '_logged_execution_row_steps'):
                            self._logged_execution_row_steps = set()
                        if clean_name not in self._logged_execution_row_steps:
                            try:
                                row_repr = dict(execution_wf) if hasattr(execution_wf, 'keys') else execution_wf
                            except Exception:
                                row_repr = repr(execution_wf)
                            self._logged_execution_row_steps.add(clean_name)

                        job_tag = None
                        for key in ('name', 'job_name', 'tag', 'job_tag'):
                            try:
                                v = execution_wf[key] if hasattr(execution_wf, 'keys') else getattr(execution_wf, key, None)
                            except Exception:
                                v = None
                            if v:
                                job_tag = str(v)
                                break
                        if not job_tag:
                            job_id = None
                            for key in ('job', 'job_id'):
                                try:
                                    job_id = execution_wf[key] if hasattr(execution_wf, 'keys') else getattr(execution_wf, key, None)
                                except Exception:
                                    job_id = None
                                if job_id:
                                    break
                            if job_id is not None:
                                for getter_name in ('get_job', 'get_job_by_id'):
                                    getter = getattr(self.context.database, getter_name, None)
                                    if getter:
                                        try:
                                            job_row = await getter(job_id)
                                            job_tag = job_row.get('name') if hasattr(job_row, 'get') else getattr(job_row, 'name', None)
                                        except Exception:
                                            job_tag = None
                                        if job_tag:
                                            break
                        task._job_name = job_tag

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
                            
                            if ("__" in port_label or port_label.startswith("_") or "job" in label_low or 
                                "-injector" in label_low or "-collector" in label_low or "token" in label_low): 
                                continue

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
                            
                            data_id = f"ent_out_{hashlib.md5(f'{clean_name}_{port_label}'.encode()).hexdigest()[:8]}"
                            data_out = Data(data_id, port_label)
                            dt, dv, dl = self._extract_port_metadata(output_port)
                            data_out._type, data_out._value, data_out._location = dt, dv, dl
                            
                            task.add_output(data_out)
                            data_out.set_producer(task._id)

            logger.info(f"YPROV [DB_EXTRACT]: Parsed {task_count} tasks in {time.perf_counter() - t0:.4f}s")
            t_cwl = time.perf_counter()

            # Resolve structural dependencies
            self.computed_cwl_deps = self._parse_cwl_for_dependencies()
            logger.info(f"YPROV [PARSER]: CWL graph parsing completed in {time.perf_counter() - t_cwl:.4f}s")

            return self.prov_workflow

    async def create_archive(self, outdir: str, filename: Optional[str], config: Optional[str], additional_files: Optional[MutableSequence[MutableMapping[str, str]]], additional_properties: Optional[MutableSequence[MutableMapping[str, str]]]):
        """
        Generates the PROV-JSON representation, performs structural edge reconciliation, 
        and packages the results into a ZIP archive.
        """
        t_total_start = time.perf_counter()

        if config is not None: 
            self.map_file["config"] = config
            
        self.prov_workflow = await self.populate_prov_workflow() 
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, filename or (self.workflows[0].name + ".zip"))
        json_file_path = self.prov_workflow.prov_to_json()  

        t_reconcile = time.perf_counter()
        try:
            logger.info("YPROV [ARCHIVE]: Reconciling graph edges into PROV-JSON schema...")
            with open(json_file_path, 'r') as f: 
                prov_data = json.load(f)

            # -----------------------------------------------------------
            # STRICT WHITELIST & ENTITY FILTERING
            # -----------------------------------------------------------
            valid_step_names = set(self.tasks_by_step_name.keys())
            valid_step_names.update([f"/{k.lstrip('/')}" for k in self.tasks_by_step_name.keys()])
            valid_step_names.update([k.lstrip('/') for k in self.tasks_by_step_name.keys()])

            def _is_system_spur(s: str) -> bool:
                if not s:
                    return False
                s_low = str(s).lower()
                return ("__" in str(s) or "job" in s_low or "-injector" in s_low or 
                        "-collector" in s_low or "-token-transformer" in s_low or 
                        "-scatter" in s_low or "-condition" in s_low)

            def is_valid_cwl_activity(label: str) -> bool:
                if not label or _is_system_spur(label):
                    return False
                clean_lbl = label.lstrip('/')
                return clean_lbl in valid_step_names or any(clean_lbl.startswith(f"{s}/") for s in valid_step_names)

            def _get_prov_id(val: Any) -> Optional[str]:
                if isinstance(val, str):
                    return val
                if isinstance(val, dict):
                    return val.get("$") or val.get("prov:id")
                return None

            def _get_prov_label(val: Any) -> str:
                if isinstance(val, dict):
                    label = val.get("prov:label") or val.get("yprov:name") or val.get("yprov:label") or ""
                    if isinstance(label, dict):
                        return label.get("$", "")
                    return str(label)
                return str(val) if val else ""

            # Purge non-CWL activities
            valid_activity_ids = set()
            activities = prov_data.get("activity", {})
            for act_id, act_val in list(activities.items()):
                label = _get_prov_label(act_val)
                if not label:
                    for v in act_val.values() if isinstance(act_val, dict) else []:
                        v_str = _get_prov_id(v) or str(v)
                        if is_valid_cwl_activity(v_str):
                            label = v_str
                            break
                
                if label and is_valid_cwl_activity(label):
                    valid_activity_ids.add(act_id)
                else:
                    del activities[act_id]

            # Filter out system spur entities
            entities = prov_data.get("entity", {})
            candidate_entity_ids = set()
            for ent_id, ent_val in list(entities.items()):
                label = _get_prov_label(ent_val) or ent_id
                if _is_system_spur(label) or _is_system_spur(ent_id):
                    del entities[ent_id]
                else:
                    candidate_entity_ids.add(ent_id)

            # Clean up relations referring to purged activities or spur entities
            referenced_entity_ids = set()

            for r_type in ["used", "wasGeneratedBy"]:
                if r_type in prov_data:
                    cleaned_rels = {}
                    for rel_id, rel_val in prov_data[r_type].items():
                        if not isinstance(rel_val, dict):
                            continue
                        act_id = _get_prov_id(rel_val.get("prov:activity"))
                        ent_id = _get_prov_id(rel_val.get("prov:entity"))
                        
                        if act_id in valid_activity_ids and ent_id in candidate_entity_ids:
                            cleaned_rels[rel_id] = rel_val
                            if ent_id:
                                referenced_entity_ids.add(ent_id)
                    prov_data[r_type] = cleaned_rels

            if "wasAssociatedWith" in prov_data:
                prov_data["wasAssociatedWith"] = {
                    k: v for k, v in prov_data["wasAssociatedWith"].items()
                    if isinstance(v, dict) and _get_prov_id(v.get("prov:activity")) in valid_activity_ids
                }

            if "wasDerivedFrom" in prov_data:
                cleaned_derived = {}
                for rel_id, rel_val in prov_data["wasDerivedFrom"].items():
                    if not isinstance(rel_val, dict):
                        continue
                    gen_ent = _get_prov_id(rel_val.get("prov:generatedEntity"))
                    used_ent = _get_prov_id(rel_val.get("prov:usedEntity"))
                    if gen_ent in referenced_entity_ids and used_ent in referenced_entity_ids:
                        cleaned_derived[rel_id] = rel_val
                prov_data["wasDerivedFrom"] = cleaned_derived

            # Clean up orphan / unreferenced entities
            if "entity" in prov_data:
                prov_data["entity"] = {
                    ent_id: ent_val for ent_id, ent_val in prov_data["entity"].items()
                    if ent_id in referenced_entity_ids
                }

            # -----------------------------------------------------------
            # EDGE RECONCILIATION & COMPOSITE BOUNDARY RESOLUTION
            # -----------------------------------------------------------
            prov_data["wasInformedBy"] = {} 
            existing_relations = set()

            def _inject_edge(p_uuid: str, c_uuid: str) -> None:
                if p_uuid != c_uuid and (c_uuid, p_uuid) not in existing_relations:
                    if p_uuid in valid_activity_ids and c_uuid in valid_activity_ids:
                        rel_key = f"_:informed_{hashlib.md5(f'{c_uuid}{p_uuid}'.encode()).hexdigest()[:8]}"
                        prov_data["wasInformedBy"][rel_key] = {"prov:informed": c_uuid, "prov:informant": p_uuid}
                        existing_relations.add((c_uuid, p_uuid))

            # Full set of CWL paths (keys + every parent listed), used to tell a
            # composite (subworkflow) path from a true leaf/atomic step.
            all_cwl_paths = set(self.computed_cwl_deps.keys())
            for _parent_list in self.computed_cwl_deps.values():
                all_cwl_paths.update(_parent_list)

            def is_composite(path: str) -> bool:
                clean_p = path.lstrip('/')
                prefix = f"{clean_p}/"
                return any(p.lstrip('/').startswith(prefix) for p in all_cwl_paths if p != path)

            def get_leaf_descendants(path: str) -> List[str]:
                clean_p = path.lstrip('/')
                if not is_composite(path):
                    return [path]
                prefix = f"{clean_p}/"
                leaves = [p for p in all_cwl_paths if p.lstrip('/').startswith(prefix) and not is_composite(p)]
                return leaves if leaves else [path]

            def get_entry_leaves(path: str) -> List[str]:
                """
                The leaf(s) of a composite that have no parent WITHIN the same
                composite -- i.e. where external data actually flows in. Without
                this, resolving a composite (e.g. '/band/wdiff') directly through
                _get_exact_leaf_tasks()'s prefix fallback returns EVERY descendant
                leaf, not just the true entry point -- which is exactly what wires
                a parent up to internal leaves it never actually feeds (e.g.
                extract_bid -> mDiffFit/check_mDiffFit_out, when only
                extract_bid -> get_diff is real).
                """
                if not is_composite(path):
                    return [path]
                leaves = get_leaf_descendants(path)
                leaf_set = set(leaves)
                entry_leaves = []
                for leaf in leaves:
                    parents = self.computed_cwl_deps.get(leaf, [])
                    has_internal_parent = any(
                        any(pl in leaf_set for pl in get_leaf_descendants(p))
                        for p in parents
                    )
                    if not has_internal_parent:
                        entry_leaves.append(leaf)
                return entry_leaves if entry_leaves else leaves

            def get_exit_leaves(path: str) -> List[str]:
                """The leaf(s) of a composite that nothing else INSIDE it depends on
                -- i.e. where the composite's real output comes from. Symmetric to
                get_entry_leaves(); same rationale (e.g. '/band' must resolve to
                just mViewer, not every band-internal step, when feeding get_rgb)."""
                if not is_composite(path):
                    return [path]
                leaves = get_leaf_descendants(path)
                leaf_set = set(leaves)
                exit_leaves = []
                for leaf in leaves:
                    is_dep_for_internal = False
                    for child, parents in self.computed_cwl_deps.items():
                        child_leaves = get_leaf_descendants(child)
                        if any(cl in leaf_set for cl in child_leaves) and child != leaf:
                            if any(leaf in get_leaf_descendants(p) for p in parents):
                                is_dep_for_internal = True
                                break
                    if not is_dep_for_internal:
                        exit_leaves.append(leaf)
                return exit_leaves if exit_leaves else leaves

            def _get_exact_leaf_tasks(step_name: str) -> List[Task]:
                clean_s = step_name.lstrip('/')
                
                # 1. Exact match lookup
                exact_match = (
                    self.tasks_by_step_name.get(clean_s) 
                    or self.tasks_by_step_name.get(f"/{clean_s}")
                )
                if exact_match:
                    return [t for t in exact_match if t._id in valid_activity_ids]
                    
                # 2. Sub-workflow prefix fallback (e.g., 'band/wdiff' -> 'band/wdiff/...')
                prefix_tasks = []
                prefix_key = f"{clean_s}/"
                for s_name, tasks in self.tasks_by_step_name.items():
                    clean_key = s_name.lstrip('/')
                    if clean_key.startswith(prefix_key):
                        prefix_tasks.extend([t for t in tasks if t._id in valid_activity_ids])
                        
                return prefix_tasks

            def _get_task_sort_key(task: Any) -> Tuple[Tuple[str, ...], Tuple[str, ...], str]:
                """Deterministic sorting key based on input/output entity identifiers."""
                inputs = getattr(task, 'inputs', getattr(task, '_inputs', []))
                outputs = getattr(task, 'outputs', getattr(task, '_outputs', []))
                
                in_keys = sorted([
                    str(getattr(d, '_location', None) or getattr(d, '_value', None) or getattr(d, '_id', ''))
                    for d in inputs
                ])
                out_keys = sorted([
                    str(getattr(d, '_location', None) or getattr(d, '_value', None) or getattr(d, '_id', ''))
                    for d in outputs
                ])
                return (tuple(in_keys), tuple(out_keys), getattr(task, '_id', ''))

            def _get_entity_keys(task: Any, is_output: bool) -> set:
                items = getattr(task, 'outputs' if is_output else 'inputs', getattr(task, '_outputs' if is_output else '_inputs', []))
                keys = set()
                for d in items:
                    loc = getattr(d, '_location', None)
                    val = getattr(d, '_value', None)
                    did = getattr(d, '_id', None)
                    if loc and str(loc) != "None":
                        keys.add(f"loc:{loc}")
                    if val and str(val) != "None":
                        keys.add(f"val:{val}")
                    if did:
                        keys.add(f"id:{did}")
                return keys

            # Map relations across step boundaries and composite subworkflows.
            # Resolve each composite path down to its true entry/exit leaf(s)
            # FIRST -- calling _get_exact_leaf_tasks directly on a composite path
            # (child_path/parent_path) falls into its prefix-fallback, which
            # returns every descendant leaf, not just the true boundary.
            for child_path, parent_paths in self.computed_cwl_deps.items():
                child_entries = get_entry_leaves(child_path)

                for parent_path in parent_paths:
                    parent_exits = get_exit_leaves(parent_path)

                    for p_leaf in parent_exits:
                        parent_tasks = _get_exact_leaf_tasks(p_leaf)
                        if not parent_tasks:
                            continue

                        for c_leaf in child_entries:
                            child_tasks = _get_exact_leaf_tasks(c_leaf)
                            if not child_tasks:
                                continue

                            p_len, c_len = len(parent_tasks), len(child_tasks)

                            if p_len == 1 and c_len > 1:
                                for c_task in child_tasks:
                                    _inject_edge(parent_tasks[0]._id, c_task._id)

                            elif c_len == 1 and p_len > 1:
                                for p_task in parent_tasks:
                                    _inject_edge(p_task._id, child_tasks[0]._id)

                            elif p_len == c_len:
                                p_tags = {id(pt): _get_scatter_tag(pt) for pt in parent_tasks}
                                c_tags = {id(ct): _get_scatter_tag(ct) for ct in child_tasks}
                                if all(p_tags.values()) and all(c_tags.values()):
                                    for ct in child_tasks:
                                        c_tag = c_tags[id(ct)]
                                        for pt in parent_tasks:
                                            p_tag = p_tags[id(pt)]
                                            if _tags_share_lineage(p_tag, c_tag) or _tags_are_compatible(getattr(pt, '_job_name', ''), getattr(ct, '_job_name', '')):
                                                _inject_edge(pt._id, ct._id)
                                else:
                                    # Sort tasks deterministically prior to zipping
                                    sorted_parents = sorted(parent_tasks, key=_get_task_sort_key)
                                    sorted_children = sorted(child_tasks, key=_get_task_sort_key)
                                    for p_task, c_task in zip(sorted_parents, sorted_children):
                                        _inject_edge(p_task._id, c_task._id)

                            else:
                                p_tags = {id(pt): _get_scatter_tag(pt) for pt in parent_tasks}
                                c_tags = {id(ct): _get_scatter_tag(ct) for ct in child_tasks}

                                if all(p_tags.values()) and all(c_tags.values()):
                                    for ct in child_tasks:
                                        c_tag = c_tags[id(ct)]
                                        for pt in parent_tasks:
                                            p_tag = p_tags[id(pt)]
                                            if _tags_share_lineage(p_tag, c_tag) or _tags_are_compatible(getattr(pt, '_job_name', ''), getattr(ct, '_job_name', '')):
                                                _inject_edge(pt._id, ct._id)
                                else:
                                    # Match mismatched scatter widths (e.g. 625 vs 300) via shared entity keys
                                    matched_any = False
                                    for p_task in parent_tasks:
                                        p_keys = _get_entity_keys(p_task, is_output=True)
                                        if not p_keys:
                                            continue

                                        for c_task in child_tasks:
                                            c_keys = _get_entity_keys(c_task, is_output=False)
                                            
                                            if p_keys & c_keys:
                                                _inject_edge(p_task._id, c_task._id)
                                                matched_any = True

                                    # Fallback to cross-product only if no entity overlaps matched
                                    if not matched_any:
                                        for p_task in parent_tasks:
                                            for c_task in child_tasks:
                                                if _tags_are_compatible(getattr(p_task, '_job_name', ''), getattr(c_task, '_job_name', '')):
                                                    _inject_edge(p_task._id, c_task._id)

            with open(json_file_path, 'w') as f: 
                json.dump(prov_data, f, indent=4)

            json_bytes = os.path.getsize(json_file_path)
            logger.info(f"YPROV [ARCHIVE]: [PROV_FILE_SIZE] provenance.json = {json_bytes} Bytes")
            logger.info(f"YPROV [ARCHIVE]: Edge reconciliation & PROV-JSON write completed in {time.perf_counter() - t_reconcile:.4f}s")

        except Exception as e:
            logger.error(f"YPROV [ARCHIVE]: Internal sync failure during JSON edge cleanup: {e}\n{traceback.format_exc()}")

        t_zip = time.perf_counter()
        with ZipFile(path, "w") as archive:
            archive.write(json_file_path, arcname="provenance.json")  
            for src, dst in self.map_file.items():
                if os.path.exists(src) and dst not in archive.namelist(): 
                    archive.write(src, dst)

        zip_bytes = os.path.getsize(path)
        logger.info(f"YPROV [ARCHIVE]: Archive compression completed in {time.perf_counter() - t_zip:.4f}s")
        logger.info(f"YPROV [ARCHIVE]: [PROV_ARCHIVE_SIZE] {os.path.basename(path)} = {zip_bytes} Bytes")
        logger.info(f"YPROV [ARCHIVE]: [TOTAL_OFFLINE_TIME] total_duration_s={time.perf_counter() - t_total_start:.4f}s")
        logger.info(f"YPROV [ARCHIVE]: Successfully generated offline yProv4WFs archive at {path}")