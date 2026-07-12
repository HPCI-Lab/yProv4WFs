"""
This module acts as an "online" progressive executor plugin for the StreamFlow
Workflow Management System (WMS). It substitutes the core scheduler engine (`streamflow/workflow/executor.py`)
to manage task executions.

It mirrors the logic used for the offline version to ensure a correct output.

To enable a simple switching between the original and plugin version, a python environment 
parameter is used as follows:

If you want to run the ORIGINAL version:
- Run streamflow run as usual
- Run USE_YPROV=false streamflow run

If you want to run the PLUGIN version:
- Run exactly USE_YPROV=true streamflow run

The default behavior is the usage of the original version.
"""

from __future__ import annotations

import asyncio
import time
import os
import sys
import uuid
import json
import yaml
import atexit
import logging
import hashlib
from collections.abc import MutableMapping, MutableSequence
from typing import TYPE_CHECKING, cast, Optional, Set, List, Tuple
from urllib.parse import urlparse, unquote
from zipfile import ZipFile

from streamflow.core import utils
from streamflow.core import utils as sf_utils
from streamflow.core.exception import WorkflowExecutionException
from streamflow.core.workflow import Executor, Status
from streamflow.log_handler import logger
from streamflow.workflow.token import TerminationToken
from streamflow.workflow.utils import get_token_value

from yprov4wfs.datamodel.workflow import Workflow as YProvWorkflow
from yprov4wfs.datamodel.task import Task as YProvTask
from yprov4wfs.datamodel.data import Data as YProvData

if TYPE_CHECKING:
    from typing import Any
    from streamflow.core.workflow import Workflow

# Silence aiosqlite background logging
logging.getLogger("aiosqlite").setLevel(logging.WARNING)

# The original version run by default if unless specified differently
if os.getenv("USE_YPROV", "").lower() == "true":
    #--------------------------------------------
    # YPROV PLUGIN INTEGRATION
    #--------------------------------------------

    def discover_workflow_cwl_files(streamflow_config_path: Optional[str]) -> list[str]:
        main_cwl = None
        if streamflow_config_path and os.path.exists(streamflow_config_path):
            try:
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
            return []

        to_parse = [os.path.realpath(main_cwl)]
        discovered_files = set(to_parse)

        def extract_run_paths(data):
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
            except Exception as e:
                logger.warning(f"YPROV Warning: Could not deep-parse {current_file}: {e}")
        
        logger.info(f"YPROV: Normalized files selected for analysis: {list(discovered_files)}")
        return list(discovered_files)


    class StreamFlowExecutor(Executor):
        """
        Custom scheduler mapping directly to StreamFlow's execution flow.
        Intercepts the operational loop to dynamically gather telemetry constraints,
        query state inputs from internal relational databases and serialize PROV-JSON.
        """
        def __init__(self, workflow: Workflow):
            super().__init__(workflow)
            self.executions: MutableSequence[asyncio.Task] = []
            self.output_tasks: MutableMapping[str, asyncio.Task] = {}
            self.received: MutableSequence[str] = []
            self.closed: bool = False

            logger.info("YPROV: Starting and loading workflows...")

            # --- yProv4Wfs state ---
            self.map_file: MutableMapping[str, str] = {}
            self.prov_workflow = None
            self.tasks_by_step_name = {}
            self.completed_step_paths: Set[str] = set()
            self.computed_cwl_deps = {}
            self.streamflow_config_path = "streamflow.yml"
            self.map_file["config"] = self.streamflow_config_path
            self.outdir = "./outputs"

        def _get_action_status(self, status: Status) -> str:
            """Maps framework internal execution status enumerations to controlled strings."""
            if status == Status.COMPLETED: return "Completed"
            elif status == Status.FAILED: return "Failed"
            elif status in [Status.CANCELLED, Status.SKIPPED]: return "Cancelled or Skipped"
            return "Running"

        def _extract_port_metadata(self, port_db_record: Any) -> Tuple[str, str, str]:
            """
            Parses database JSON records or dictionary attributes tracking data ports
            to safely extract the structural datatype, value reference, and file location metadata.
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
                    logger.warning(f"YPROV: Error reading file {filename}: {e}")
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
                                
            # Locate the main root workflow directly from streamflow.yml
            main_workflow_file = None
            
            if streamflow_config_path and os.path.exists(streamflow_config_path):
                try:
                    with open(streamflow_config_path, 'r') as sf:
                        sf_data = yaml.safe_load(sf)
                    
                    # Dig down into workflows -> config -> file
                    workflows_sec = sf_data.get('workflows', {})
                    for wf_name, wf_val in workflows_sec.items():
                        wf_config = wf_val.get('config', {})
                        wf_file_path = wf_config.get('file')
                        if wf_file_path:
                            main_workflow_file = os.path.basename(wf_file_path)
                            logger.info(f"YPROV: Extracted master root workflow from streamflow.yml: {main_workflow_file}")
                            break
                except Exception as e:
                    logger.warning(f"YPROV: Failed reading streamflow.yml for main entrypoint: {e}")

            # Final safety fallback just in case the file reading fails or structure is unexpected
            if not main_workflow_file:
                for base_name, data in cwl_registry.items():
                    if data.get('class') == 'Workflow':
                        main_workflow_file = base_name
                        break

            # Kick off parsing
            if main_workflow_file:
                logger.info(f"YPROV: Starting hierarchical parsing from root workflow entry point: {main_workflow_file}")
                extract_steps_recursive(cwl_registry[main_workflow_file], current_prefix="")
            else:
                logger.warning("YPROV: Failed to locate a primary master Workflow file to analyze.")

            logger.info(f"YPROV Dependencies list computed: {dependencies}")
            return dependencies

        async def _monitored_step_run(self, step: Any, task_name: str):
            """Passes scheduling handles straight to StreamFlow to maintain maximum execution speed."""
            await step.run()

        async def _handle_exception(self, task: asyncio.Task):
            try:
                return await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.exception(exc)
                if not self.closed:
                    await self._shutdown()

        async def _shutdown(self):
            await asyncio.gather(
                *(
                    asyncio.create_task(step.terminate(Status.CANCELLED))
                    for step in self.workflow.steps.values()
                    if not step.terminated
                )
            )
            self.closed = True

        async def _wait_outputs(
            self, output_consumer: str, output_tokens: MutableMapping[str, Any]
        ) -> MutableMapping[str, Any]:
            finished, unfinished = await asyncio.wait(
                self.output_tasks.values(), return_when=asyncio.FIRST_COMPLETED
            )
            self.output_tasks = {t.get_name(): t for t in unfinished}
            for task in finished:
                if task.cancelled(): continue
                task_name = cast(asyncio.Task, task).get_name()
                if task_name not in self.workflow.output_ports: continue
                token = task.result()
                if isinstance(token, TerminationToken):
                    if token.value in (Status.CANCELLED, Status.FAILED):
                        self.closed = True
                        for t in unfinished: t.cancel()
                        return output_tokens
                    else:
                        self.received.append(task_name)
                        if len(self.received) == len(self.workflow.output_ports):
                            self.closed = True
                else:
                    output_tokens[task_name] = get_token_value(token)
                    if task_name not in self.received:
                        self.output_tasks[task_name] = asyncio.create_task(
                            self._handle_exception(
                                asyncio.create_task(
                                    self.workflow.get_output_port(task_name).get(output_consumer)
                                )
                            ),
                            name=task_name,
                        )
            for port_name, port in self.workflow.get_output_ports().items():
                if port_name not in self.output_tasks and port_name not in self.received:
                    self.output_tasks[port_name] = asyncio.create_task(
                        self._handle_exception(asyncio.create_task(port.get(output_consumer))),
                        name=port_name,
                    )
                    self.closed = False
            return output_tokens

        async def run(self) -> MutableMapping[str, Any]:
            """
            Executes the workflow graph. Once execution finishes, it uses an in-memory aligned
            extraction model to capture scattered task states, align them with CWL 
            dependencies, apply filtering rules and bundle the final archive.
            """
            try:
                output_tokens = {}
                logger.info(f"Workflow ID {self.workflow.persistent_id}")

                await self.workflow.context.database.update_workflow(
                    self.workflow.persistent_id, {"start_time": time.time_ns()}
                )

                for task_name, step in self.workflow.steps.items():
                    execution = asyncio.create_task(
                        self._handle_exception(asyncio.create_task(self._monitored_step_run(step, task_name))),
                        name=step.name,
                    )
                    self.executions.append(execution)

                if self.workflow.persistent_id:
                    await self.workflow.context.database.update_workflow(
                        self.workflow.persistent_id, {"status": Status.RUNNING.value}
                    )

                if self.workflow.output_ports:
                    output_consumer = utils.random_name()
                    for port_name, port in self.workflow.get_output_ports().items():
                        self.output_tasks[port_name] = asyncio.create_task(
                            self._handle_exception(asyncio.create_task(port.get(output_consumer))),
                            name=port_name,
                        )
                    while not self.closed:
                        output_tokens = await self._wait_outputs(output_consumer, output_tokens)
                else:
                    await asyncio.gather(*self.executions)

                if self.executions:
                    logger.info("YPROV: Synchronizing remaining background steps with a safety timeout...")
                    done, pending = await asyncio.wait(self.executions, timeout=3.0)
                    if pending:
                        logger.warning(f"YPROV: {len(pending)} step tasks did not join within 3s. Proceeding with serialization.")

                for step in self.workflow.steps.values():
                    if step.status in [Status.FAILED, Status.CANCELLED]:
                        raise WorkflowExecutionException("FAILED Workflow execution")

                if self.workflow.persistent_id:
                    await self.workflow.context.database.update_workflow(
                        self.workflow.persistent_id,
                        {"status": Status.COMPLETED.value, "end_time": time.time_ns()},
                    )
                
                # ======================================================================
                # EXTRACTION BLOCK
                # ======================================================================
                logger.info("YPROV: Performing database extraction for exact offline parity...")
                self.tasks_by_step_name = {}
                wf = self.workflow
                wf_obj = await self.workflow.context.database.get_workflow(wf.persistent_id)
                
                self.prov_workflow = YProvWorkflow(wf_obj["name"], f'workflow_{wf_obj["name"]}')
                self.prov_workflow._start_time = sf_utils.get_date_from_ns(wf_obj["start_time"])
                self.prov_workflow._end_time = sf_utils.get_date_from_ns(wf_obj["end_time"])
                self.prov_workflow._status = self._get_action_status(Status(wf_obj["status"]))
                self.prov_workflow._engineWMS = 'StreamFlow'
                self.prov_workflow._level = '0'
                
                if "config" in self.map_file: 
                    self.prov_workflow._resource_cwl_uri = self.map_file["config"]

                for task_name in wf.steps:
                    clean_name = task_name.lstrip('/')
                    if s := wf.steps.get(task_name):
                        executions = await self.workflow.context.database.get_executions_by_step(s.persistent_id)
                        
                        for execution_wf in executions:
                            task = YProvTask(str(uuid.uuid4()), clean_name)
                            task._start_time = sf_utils.get_date_from_ns(execution_wf["start_time"])
                            task._end_time = sf_utils.get_date_from_ns(execution_wf["end_time"])
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

                            inputs = await self.workflow.context.database.get_input_ports(s.persistent_id)
                            for input_port in inputs:
                                port_label = input_port["name"].split('/')[-1]
                                label_low = port_label.lower()
                                
                                if ("__" in port_label or port_label.startswith("_") or "job" in label_low or 
                                    "-injector" in label_low or "-collector" in label_low or "token" in label_low): 
                                    continue
                                
                                data_id = f"ent_in_{hashlib.md5(f'{clean_name}_{port_label}'.encode()).hexdigest()[:8]}"
                                data_in = YProvData(data_id, port_label)
                                dt, dv, dl = self._extract_port_metadata(input_port)
                                data_in._type, data_in._value, data_in._location = dt, dv, dl
                                
                                task.add_input(data_in)
                                data_in.add_consumer(task._id)

                            outputs = await self.workflow.context.database.get_output_ports(s.persistent_id)
                            for output_port in outputs:
                                port_label = output_port["name"].split('/')[-1]
                                label_low = port_label.lower()
                                
                                if ("__" in port_label or port_label.startswith("_") or "job" in label_low or 
                                    "-injector" in label_low or "-collector" in label_low or "token" in label_low): 
                                    continue
                                
                                data_id = f"ent_out_{hashlib.md5(f'{clean_name}_{port_label}'.encode()).hexdigest()[:8]}"
                                data_out = YProvData(data_id, port_label)
                                dt, dv, dl = self._extract_port_metadata(output_port)
                                data_out._type, data_out._value, data_out._location = dt, dv, dl
                                
                                task.add_output(data_out)
                                data_out.set_producer(task._id)

                # Compute Ahead-of-Time dependencies 
                self.computed_cwl_deps = self._parse_cwl_for_dependencies()

                os.makedirs(self.outdir, exist_ok=True)
                json_file_path = self.prov_workflow.prov_to_json()  
                
                try:
                    with open(json_file_path, 'r') as f:
                        prov_data = json.load(f)
                    
                    prov_data["wasInformedBy"] = {} 
                    existing_relations = set()

                    def _inject_edge(p_uuid: str, c_uuid: str) -> None:
                        if p_uuid != c_uuid and (c_uuid, p_uuid) not in existing_relations:
                            rel_key = f"_:informed_{hashlib.md5(f'{c_uuid}{p_uuid}'.encode()).hexdigest()[:8]}"
                            prov_data["wasInformedBy"][rel_key] = {"prov:informed": c_uuid, "prov:informant": p_uuid}
                            existing_relations.add((c_uuid, p_uuid))

                    # ALIGNED COPIED LOGIC: Added nested leaf resolution 
                    def _resolve_leaf_tasks(step_name: str) -> List[Any]:
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

                    # ALIGNED COPIED LOGIC: Loop now safely evaluates multi-tier sub-workflow paths
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

                except Exception as e:
                    logger.error(f"YPROV: Internal synchronization error occurred while creating archive: {e}")

                path = os.path.join(self.outdir, self.workflow.name + ".zip")
                with ZipFile(path, "w") as archive:
                    archive.write(json_file_path, arcname="provenance.json")  
                    for src, dst in self.map_file.items():
                        if os.path.exists(src) and dst not in archive.namelist():
                            archive.write(src, dst)
                
                print(f"YPROV: Successfully zipped runtime profile package entry at {path}")
                
                try:
                    import concurrent.futures.process
                    atexit.unregister(concurrent.futures.process._python_exit)
                except Exception:
                    pass

                logger.info("YPROV: Yielding to StreamFlow for final logging. Output tokens returned.")
                
                import threading
                def delayed_exit():
                    time.sleep(1.0)
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os._exit(0)
                
                threading.Thread(target=delayed_exit, daemon=True).start()
                return output_tokens

            except Exception:
                if self.workflow.persistent_id:
                    await self.workflow.context.database.update_workflow(
                        self.workflow.persistent_id,
                        {"status": Status.FAILED.value, "end_time": time.time_ns()},
                    )
                if not self.closed:
                    await self._shutdown()
                raise

else:
    #--------------------------------------------
    # ORIGINAL VERSION
    #--------------------------------------------
    class StreamFlowExecutor(Executor):
        def __init__(self, workflow: Workflow):
            super().__init__(workflow)
            self.executions: MutableSequence[asyncio.Task] = []
            self.output_tasks: MutableMapping[str, asyncio.Task] = {}
            self.received: MutableSequence[str] = []
            self.closed: bool = False

        async def _handle_exception(self, task: asyncio.Task):
            try:
                return await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.exception(exc)
                if not self.closed:
                    await self._shutdown()

        async def _shutdown(self):
            await asyncio.gather(
                *(
                    asyncio.create_task(step.terminate(Status.CANCELLED))
                    for step in self.workflow.steps.values()
                    if not step.terminated
                )
            )
            self.closed = True

        async def _wait_outputs(
            self, output_consumer: str, output_tokens: MutableMapping[str, Any]
        ) -> MutableMapping[str, Any]:
            finished, unfinished = await asyncio.wait(
                self.output_tasks.values(), return_when=asyncio.FIRST_COMPLETED
            )
            self.output_tasks = {t.get_name(): t for t in unfinished}
            for task in finished:
                if task.cancelled():
                    continue
                task_name = cast(asyncio.Task, task).get_name()
                if task_name not in self.workflow.output_ports:
                    continue
                token = task.result()
                if isinstance(token, TerminationToken):
                    if token.value in (Status.CANCELLED, Status.FAILED):
                        self.closed = True
                        for t in unfinished:
                            t.cancel()
                        return output_tokens
                    else:
                        self.received.append(task_name)
                        if len(self.received) == len(self.workflow.output_ports):
                            self.closed = True
                else:
                    output_tokens[task_name] = get_token_value(token)
                    if task_name not in self.received:
                        self.output_tasks[task_name] = asyncio.create_task(
                            self._handle_exception(
                                asyncio.create_task(
                                    self.workflow.get_output_port(task_name).get(
                                        output_consumer
                                    )
                                )
                            ),
                            name=task_name,
                        )
            for port_name, port in self.workflow.get_output_ports().items():
                if port_name not in self.output_tasks and port_name not in self.received:
                    self.output_tasks[port_name] = asyncio.create_task(
                        self._handle_exception(
                            asyncio.create_task(port.get(output_consumer))
                        ),
                        name=port_name,
                    )
                    self.closed = False
            return output_tokens

        async def run(self) -> MutableMapping[str, Any]:
            try:
                output_tokens = {}
                await self.workflow.context.database.update_workflow(
                    self.workflow.persistent_id, {"start_time": time.time_ns()}
                )
                for step in self.workflow.steps.values():
                    execution = asyncio.create_task(
                        self._handle_exception(asyncio.create_task(step.run())),
                        name=step.name,
                    )
                    self.executions.append(execution)
                if self.workflow.persistent_id:
                    await self.workflow.context.database.update_workflow(
                        self.workflow.persistent_id, {"status": Status.RUNNING.value}
                    )
                if self.workflow.output_ports:
                    output_consumer = utils.random_name()
                    for port_name, port in self.workflow.get_output_ports().items():
                        self.output_tasks[port_name] = asyncio.create_task(
                            self._handle_exception(
                                asyncio.create_task(port.get(output_consumer))
                            ),
                            name=port_name,
                        )
                    while not self.closed:
                        output_tokens = await self._wait_outputs(
                            output_consumer, output_tokens
                        )
                else:
                    await asyncio.gather(*self.executions)
                for step in self.workflow.steps.values():
                    if step.status in [Status.FAILED, Status.CANCELLED]:
                        raise WorkflowExecutionException("FAILED Workflow execution")
                if self.workflow.persistent_id:
                    await self.workflow.context.database.update_workflow(
                        self.workflow.persistent_id,
                        {"status": Status.COMPLETED.value, "end_time": time.time_ns()},
                    )

                import threading
                def delayed_exit():
                    time.sleep(1.0)
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os._exit(0)
                
                threading.Thread(target=delayed_exit, daemon=True).start()

                return output_tokens
            except Exception:
                if self.workflow.persistent_id:
                    await self.workflow.context.database.update_workflow(
                        self.workflow.persistent_id,
                        {"status": Status.FAILED.value, "end_time": time.time_ns()},
                    )
                if not self.closed:
                    await self._shutdown()
                raise