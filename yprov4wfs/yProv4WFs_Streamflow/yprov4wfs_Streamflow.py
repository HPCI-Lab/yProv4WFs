"""
This module acts as an "online" progressive executor plugin for the StreamFlow
Workflow Management System (WMS). It substitutes the core scheduler engine (`streamflow/workflow/executor.py`)
to manage task executions.

It mirrors the logic used for the offline version to ensure a correct and comparable output.

To enable a simple switching between teh original and plugin version, a python environment 
parameter is used as follows:

If you want to run the ORIGINAL version:
- Run streamflow run as usual
- Run USE_YPROV=false streamflow run

If you want to run the PLUGIN version:
- Run exactly USE_YPROV_true streamflow run

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
from pathlib import Path
from zipfile import ZipFile

from streamflow.core import utils
from streamflow.core import utils as sf_utils
from streamflow.core.exception import WorkflowExecutionException
from streamflow.core.workflow import Executor, Status
from streamflow.log_handler import logger
from streamflow.workflow.token import TerminationToken
from streamflow.workflow.utils import get_token_value

from cwl_utils.parser import load_document_by_uri

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
        """
        Parses the primary streamflow.yml deployment config file to isolate the primary
        CWL entrypoint, then recursively crawls all dependencies and sub-workflow URLs 
        referenced across steps using cwl-utils.

        Args:
            streamflow_config_path (str): Path to streamflow.yml descriptor.

        Returns:
            list[str]: Absolute file system paths of all participating CWL manifests.
        """
        main_cwl = None
        if streamflow_config_path and os.path.exists(streamflow_config_path):
            try:
                with open(streamflow_config_path, 'r') as sf_file:
                    sf_data = yaml.safe_load(sf_file)
                
                workflows = sf_data.get("workflows", {})
                if workflows and isinstance(workflows, dict):
                    first_workflow_name = next(iter(workflows))
                    workflow_data = workflows.get(first_workflow_name, {})
                    main_cwl_relative = workflow_data.get("config", {}).get("file")
                    
                    if main_cwl_relative:
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
            if abs_path in cwl_files_paths:
                return
            cwl_files_paths.add(abs_path)
            try:
                uri = Path(abs_path).resolve().as_uri()
                doc = load_document_by_uri(uri)
                process_list = doc if isinstance(doc, list) else [doc]
                for process in process_list:
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

        discover_recursive(main_cwl)
        return list(cwl_files_paths)


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
            Inspects structural CWL workflow graphs to deduce correct task input-to-output 
            dependencies. This enables precise topological synchronization between scattered tasks.
            """
            dependencies = {}
            streamflow_config_path = self.map_file.get("config")
            cwl_files = discover_workflow_cwl_files(streamflow_config_path)

            if not cwl_files:
                logger.warning("YPROV: No active CWL files discovered via graph parsing.")
                return dependencies

            full_path_map = {}
            for full_path in self.tasks_by_step_name.keys():
                normalized_path = '/' + full_path.lstrip('/')
                short_name = normalized_path.split('/')[-1]
                full_path_map[short_name] = normalized_path

            for filename in cwl_files:
                try:
                    absolute_target_path = os.path.abspath(filename)
                    with open(absolute_target_path, 'r') as f:
                        data = yaml.safe_load(f)
                    if data.get('class') != 'Workflow': 
                        continue

                    steps = data.get('steps', {})
                    steps_items = steps.items() if isinstance(steps, dict) else [(s['id'], s) for s in steps]

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
                                    if '/' in s:
                                        parent_short_name = s.split('/')[0]
                                        full_parent_name = f"{current_prefix}/{parent_short_name}" if current_prefix else f"/{parent_short_name}"
                                        if full_parent_name in full_path_map.values() and full_parent_name not in dependencies[full_step_name]:
                                            dependencies[full_step_name].append(full_parent_name)
                                    elif current_prefix and current_prefix != '/':
                                        if current_prefix.count('/') >= 2:
                                            parent_environment = current_prefix.rsplit('/', 1)[0]
                                            for short_name, full_path in full_path_map.items():
                                                if (full_path.startswith(parent_environment) and 
                                                    not full_path.startswith(current_prefix) and 
                                                    full_path != full_step_name):
                                                    if full_path not in dependencies[full_step_name]:
                                                        dependencies[full_step_name].append(full_path)
                except Exception as e:
                    logger.warning(f"YPROV: Error parsing file {filename}: {e}")
                    continue

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
            Executes the workflow graph. Once execution finishes, it uses a database-driven 
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

                # Safety bounded timeout join loop
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
                # DATABASE-DRIVEN EXTRACTION
                # ======================================================================
                logger.info("YPROV: Performing database extraction for exact offline parity...")
                self.tasks_by_step_name = {}
                wf = self.workflow
                wf_obj = await self.workflow.context.database.get_workflow(wf.persistent_id)
                
                # Setup primary Level-0 metadata block
                self.prov_workflow = YProvWorkflow(wf_obj["name"], f'workflow_{wf_obj["name"]}')
                self.prov_workflow._start_time = sf_utils.get_date_from_ns(wf_obj["start_time"])
                self.prov_workflow._end_time = sf_utils.get_date_from_ns(wf_obj["end_time"])
                self.prov_workflow._status = self._get_action_status(Status(wf_obj["status"]))
                self.prov_workflow._engineWMS = 'StreamFlow'
                self.prov_workflow._level = '0'
                
                if "config" in self.map_file: 
                    self.prov_workflow._resource_cwl_uri = self.map_file["config"]

                # Capture every single individual scatter task chunk/iteration out of database execution records
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

                            # Extract input entities with strict offline filtering and deterministic hashing
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

                            # Extract output entities with strict offline filtering and deterministic hashing
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

                # Compute Ahead-of-Time structural workflow connections
                self.computed_cwl_deps = self._parse_cwl_for_dependencies()

                # Output the baseline serialized PROV-JSON document
                os.makedirs(self.outdir, exist_ok=True)
                json_file_path = self.prov_workflow.prov_to_json()  
                
                # Post-serialization step array mapping & purification matching offline rules
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

                    # Topological array dependency expansion loop (1:1, 1:N, N:1, or N:M fallback)
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

                    # Pure system structural filter pass to wipe out framework boilerplate nodes
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
                # ======================================================================

                # Compress final profile package into output directories
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
                
                # Smooth background exit thread to decouple locks
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
            # Terminate all steps
            await asyncio.gather(
                *(
                    asyncio.create_task(step.terminate(Status.CANCELLED))
                    for step in self.workflow.steps.values()
                    if not step.terminated
                )
            )
            # Mark the executor as closed
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
                # If a TerminationToken is received, the corresponding port terminated its outputs
                if isinstance(token, TerminationToken):
                    if token.value in (Status.CANCELLED, Status.FAILED):
                        self.closed = True
                        for t in unfinished:
                            t.cancel()
                        return output_tokens
                    else:
                        self.received.append(task_name)
                        # When the last port terminates, the entire executor terminates
                        if len(self.received) == len(self.workflow.output_ports):
                            self.closed = True
                else:
                    # Collect result
                    output_tokens[task_name] = get_token_value(token)
                    # Create a new task in place of the completed one if not terminated
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
            # Check if new output ports have been created
            for port_name, port in self.workflow.get_output_ports().items():
                if port_name not in self.output_tasks and port_name not in self.received:
                    self.output_tasks[port_name] = asyncio.create_task(
                        self._handle_exception(
                            asyncio.create_task(port.get(output_consumer))
                        ),
                        name=port_name,
                    )
                    self.closed = False
            # Return output tokens
            return output_tokens

        async def run(self) -> MutableMapping[str, Any]:
            try:
                output_tokens = {}
                # Execute workflow
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
                # If workflow has output ports
                if self.workflow.output_ports:
                    # Retrieve output tokens
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
                # Otherwise simply wait for all tasks to finish
                else:
                    await asyncio.gather(*self.executions)
                # Check if workflow terminated properly
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

                # Print output tokens
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