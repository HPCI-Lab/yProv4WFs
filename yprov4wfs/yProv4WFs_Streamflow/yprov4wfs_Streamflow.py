"""
This module acts as an "online" progressive executor plugin for the StreamFlow
Workflow Management System (WMS). It substitutes the core scheduler engine (`streamflow/workflow/executor.py`)
to manage task executions.

It mirrors the logic used for the offline version to ensure a correct output.

To enable simple switching between the original and plugin version, a python environment 
parameter is used as follows:

If you want to run the ORIGINAL version:
- Run streamflow run as usual
- Run USE_YPROV=false streamflow run

If you want to run the PLUGIN version:
- Run USE_YPROV=true streamflow run

The default behavior is the usage of the original version.

In addition, the runtime version allows the user to configure execution and 
batch writing parameters via environment variables:

Execution settings:
- BATCH_SIZE: Set the execution batch size as a positive integer (e.g., BATCH_SIZE=20). 
  Defaults to 10 if unset, invalid, or <= 0.

Batch writing settings:
- _FLUSH_BATCH_SIZE: How many tasks need to be completed before flushing.
- _FLUSH_MIN_INTERVAL_S: How many seconds need to pass before flushing (modifiable directly in the source code).

Example usage with custom flags:
- USE_YPROV=true BATCH_SIZE=25 streamflow run

Based on the workflow characteristics, it is possible to find a better set of
parameters (the one proposed should already give a good performance).
"""

from __future__ import annotations

import asyncio
import inspect
import time
import os
import sys
import uuid
import json
import yaml
import atexit
import logging
import hashlib
import traceback
from collections.abc import MutableMapping, MutableSequence
from typing import TYPE_CHECKING, cast, Optional, Set, List, Tuple
from urllib.parse import urlparse, unquote
from zipfile import ZipFile

from streamflow.core import utils
from streamflow.core import utils as sf_utils
from streamflow.core.exception import WorkflowExecutionException
from streamflow.core.workflow import Executor, Status, Step
from streamflow.log_handler import logger
from streamflow.workflow.token import TerminationToken
from streamflow.workflow.utils import get_token_value

from yprov4wfs.datamodel.workflow import Workflow as YProvWorkflow
from yprov4wfs.datamodel.task import Task as YProvTask
from yprov4wfs.datamodel.data import Data as YProvData

if TYPE_CHECKING:
    from typing import Any
    from streamflow.core.workflow import Workflow

# Silence noisy database logs
logging.getLogger("aiosqlite").setLevel(logging.WARNING)

# Environment trigger check
USE_YPROV = os.getenv("USE_YPROV", "").lower() == "true"

# Intermediate provenance writes are batched instead of happening after every
# single task. Each write re-serializes and re-purges the WHOLE graph, so
# writing once per task costs O(T^2) total for a T-task workflow. Flushing
# every _FLUSH_BATCH_SIZE tasks (or _FLUSH_MIN_INTERVAL_S seconds, whichever
# comes first) keeps the on-disk file bounded-freshness "online" while
# cutting total flush cost by roughly the batch size.

# Batch size check
try:
    _FLUSH_BATCH_SIZE = int(os.getenv("BATCH_SIZE", 10)) # tasks
    if _FLUSH_BATCH_SIZE <= 0: # Negative values are NOT valid, fallback value
        _FLUSH_BATCH_SIZE = 10
except ValueError:
    _FLUSH_BATCH_SIZE = 10  # Fallback if BATCH_SIZE is set to non-numeric text

_FLUSH_MIN_INTERVAL_S = 5.0 * 60.0 # minutes

# -------------------------------------------------------

def _yprov_log(msg: str, level: str = "info"):
    """Guaranteed unbuffered console log helper."""
    log_func = getattr(logger, level.lower(), logger.info)
    log_func(f"[YPROV ONLINE PLUGIN] {msg}")

def _is_system_spur(name: str) -> bool:
    """Check if a step, port, or entity is an internal StreamFlow runtime component."""
    clean = name.lstrip('/').lower()
    system_keywords = [
        "__",
        "token-transformer",
        "scatter-combinator",
        "scatter-size-transformer",
        "default-transformer",
        "transformer",
        "injector",
        "collector",
        "scatter",
        "combinator",
        "broadcaster",
    ]
    return any(kw in clean for kw in system_keywords)

def _generate_entity_id(step_name: str, port_label: str, p_type: str, p_val: str, p_loc: str, is_output: bool = False) -> str:
    """
    Generate deterministic Entity IDs scoped strictly to data identity or step/port.
    Excludes task execution UUIDs to ensure scatter tasks share common entities.
    """
    clean_step = step_name.lstrip('/')

    # File or directory entity with valid path/location
    if p_loc and p_loc != "None":
        return f"ent_file_{hashlib.md5(p_loc.encode()).hexdigest()[:12]}"
    
    # Value/primitive parameter (deduplicated per step & value)
    if p_val and p_val != "None":
        raw_key = f"{clean_step}_{port_label}_{p_val}"
        return f"ent_val_{hashlib.md5(raw_key.encode()).hexdigest()[:12]}"
    
    # Dynamic output port entity (shared across scatter iterations of this step)
    if is_output:
        raw_key = f"{clean_step}_{port_label}_out"
        return f"ent_out_{hashlib.md5(raw_key.encode()).hexdigest()[:12]}"

    # Input fallback port entity
    raw_key = f"{clean_step}_{port_label}_in"
    return f"ent_in_{hashlib.md5(raw_key.encode()).hexdigest()[:12]}"


if USE_YPROV:
    _yprov_log("USE_YPROV=true: INITIALIZING STREAMFLOW EXECUTOR")
    _yprov_log(f"BATCH SIZE set to {_FLUSH_BATCH_SIZE}")

    #--------------------------------------------
    # CWL DEPENDENCY PARSING HELPERS
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
                _yprov_log(f"Error reading streamflow.yml ({streamflow_config_path}): {e}", "warning")

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
                _yprov_log(f"Could not deep-parse {current_file}: {e}", "warning")
        
        _yprov_log(f"Discovered CWL target files: {list(discovered_files)}")
        return list(discovered_files)

    def _get_action_status(status: Status) -> str:
        if status == Status.COMPLETED:
            return "Completed"
        elif status == Status.FAILED:
            return "Failed"
        elif status in [Status.CANCELLED, Status.SKIPPED]:
            return "Cancelled or Skipped"
        return "Completed"

    def _extract_memory_port_metadata(port_obj: Any) -> Tuple[str, str, str]:
        p_type, p_val, p_loc = "string", "None", "None"
        try:
            tokens = getattr(port_obj, 'tokens', getattr(port_obj, '_tokens', []))
            if tokens and len(tokens) > 0:
                val = tokens[-1].data
                if isinstance(val, dict):
                    p_type = val.get("class", "File" if "path" in val or "location" in val else "string")
                    p_loc = val.get("location", val.get("path", "None"))
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

    #--------------------------------------------
    # STREAMFLOW EXECUTOR PLUGIN
    #--------------------------------------------

    class StreamFlowExecutor(Executor):
        """
        Custom StreamFlow Executor Engine. Wraps task steps natively during execution
        and flushes PROV-JSON files immediately upon step completion.
        """
        def __init__(self, workflow: Workflow):
            super().__init__(workflow)
            self.executions: MutableSequence[asyncio.Task] = []
            self.output_tasks: MutableMapping[str, asyncio.Task] = {}
            self.received: MutableSequence[str] = []
            self._closed: bool = False
            self._closing: asyncio.Event | None = None
            self.closed: bool = False

            _yprov_log("StreamFlowExecutor instance created successfully.")

            self.map_file: MutableMapping[str, str] = {}
            self.prov_workflow = None
            self.tasks_by_step_name = {}
            self.job_recorded_steps = set()
            self.computed_cwl_deps = {}
            # Dynamically intercept the target .yml / .yaml file from the run command
            yaml_args = [arg for arg in sys.argv if arg.endswith(('.yml', '.yaml'))]
            self.streamflow_config_path = yaml_args[0] if yaml_args else "streamflow.yml"
            
            self.map_file["config"] = self.streamflow_config_path
            self.outdir = "./outputs"

            # Memoizes _is_valid_cwl_step() per step name
            self._activity_validity_cache: MutableMapping[str, bool] = {}

            # Batches intermediate writes instead of flushing after every
            # single task, see _maybe_flush() for the reasoning. Fixed
            # constants, not env-configurable: USE_YPROV is the only knob.
            self._flush_batch_size = _FLUSH_BATCH_SIZE
            self._flush_min_interval_s = _FLUSH_MIN_INTERVAL_S
            self._pending_since_flush = 0
            self._last_flush_ts = 0.0

            # Serializes all writes to yprov4wfs.json and coalesces bursty
            # flush requests, see _flush_prov_json_async() / _maybe_flush().
            self._flush_lock = asyncio.Lock()
            self._flush_task: Optional[asyncio.Task] = None
            self._flush_again = False

        def _is_valid_cwl_step(self, clean_name: str) -> bool:
            """
            Whitelist match against CWL hierarchy with name normalization.

            Memoized by clean_name: this gets called once per activity on
            every flush (so hundreds of times for a heavily-scattered step),
            but a given step name's validity never changes once decided, so
            re-running the regex/normalization work every time is pure waste.
            """
            cached = self._activity_validity_cache.get(clean_name)
            if cached is not None:
                return cached

            result = self._compute_is_valid_cwl_step(clean_name)
            self._activity_validity_cache[clean_name] = result
            return result

        def _compute_is_valid_cwl_step(self, clean_name: str) -> bool:
            # Immediately drop internal StreamFlow keywords (*injector, *transformer, etc.)
            if _is_system_spur(clean_name):
                return False

            # Fallback if CWL parsing produced no steps
            if not self.computed_cwl_deps:
                return True

            import re
            # Strip scatter indices (_0, [0]) from each path segment
            parts = clean_name.lstrip('/').split('/')
            cleaned_parts = [re.sub(r'(_\d+|\[\d+\])$', '', p) for p in parts]
            normalized_path = "/" + "/".join(cleaned_parts)
            base_step_name = cleaned_parts[-1]

            # Check if normalized path or base step matches CWL dependencies
            for cwl_key in self.computed_cwl_deps.keys():
                cwl_base = cwl_key.lstrip('/').split('/')[-1]
                if cwl_key == normalized_path or cwl_base == base_step_name:
                    return True

            return False

        def _attach_step_monitors(self, step: Step):
            """Dynamically intercept low-level job execution to trace individual scatter steps."""
            clean_name = step.name.lstrip('/')
            if not self._is_valid_cwl_step(clean_name):
                return

            candidate_methods = [
                '_execute_job',
                '_run_job',
                '_execute',
                '_process_job',
                '_execute_step',
                'execute'
            ]

            for method_name in candidate_methods:
                if hasattr(step, method_name):
                    orig_method = getattr(step, method_name)
                    if callable(orig_method) and not getattr(orig_method, '_is_yprov_hook', False):
                        if inspect.iscoroutinefunction(orig_method):
                            async def wrapper(*args, **kwargs):
                                start_ns = time.time_ns()
                                try:
                                    res = await orig_method(*args, **kwargs)
                                    end_ns = time.time_ns()
                                    self._on_job_complete(step, start_ns, end_ns, status=Status.COMPLETED)
                                    return res
                                except Exception:
                                    end_ns = time.time_ns()
                                    self._on_job_complete(step, start_ns, end_ns, status=Status.FAILED)
                                    raise
                            wrapper._is_yprov_hook = True
                            setattr(step, method_name, wrapper)
                        else:
                            def wrapper(*args, **kwargs):
                                start_ns = time.time_ns()
                                try:
                                    res = orig_method(*args, **kwargs)
                                    end_ns = time.time_ns()
                                    self._on_job_complete(step, start_ns, end_ns, status=Status.COMPLETED)
                                    return res
                                except Exception:
                                    end_ns = time.time_ns()
                                    self._on_job_complete(step, start_ns, end_ns, status=Status.FAILED)
                                    raise
                            wrapper._is_yprov_hook = True
                            setattr(step, method_name, wrapper)

        def _on_job_complete(self, step: Step, start_time_ns: int, end_time_ns: int, status: Status = Status.COMPLETED):
            clean_name = step.name.lstrip('/')
            if not self._is_valid_cwl_step(clean_name):
                return

            self.job_recorded_steps.add(clean_name)
            task_id = str(uuid.uuid4())

            task = YProvTask(task_id, clean_name)
            task._start_time = sf_utils.get_date_from_ns(start_time_ns)
            task._end_time = sf_utils.get_date_from_ns(end_time_ns)
            task._status = _get_action_status(status)
            task._level = '1'

            # Extract Inputs
            in_ports = step.get_input_ports() if hasattr(step, 'get_input_ports') else getattr(step, 'input_ports', {})
            for port_name, port_obj in in_ports.items():
                port_label = port_name.split('/')[-1]
                if _is_system_spur(port_label) or _is_system_spur(port_name):
                    continue
                dt, dv, dl = _extract_memory_port_metadata(port_obj)
                data_id = _generate_entity_id(clean_name, port_label, dt, dv, dl, is_output=False)
                
                data_in = YProvData(data_id, port_label)
                data_in._type, data_in._value, data_in._location = dt, dv, dl
                task.add_input(data_in)
                data_in.add_consumer(task._id)

            # Extract Outputs
            out_ports = step.get_output_ports() if hasattr(step, 'get_output_ports') else getattr(step, 'output_ports', {})
            for port_name, port_obj in out_ports.items():
                port_label = port_name.split('/')[-1]
                if _is_system_spur(port_label) or _is_system_spur(port_name):
                    continue
                dt, dv, dl = _extract_memory_port_metadata(port_obj)
                data_id = _generate_entity_id(clean_name, port_label, dt, dv, dl, is_output=True)

                data_out = YProvData(data_id, port_label)
                data_out._type, data_out._value, data_out._location = dt, dv, dl
                task.add_output(data_out)
                data_out.set_producer(task._id)

            self.register_and_flush_task(clean_name, task)

        async def _run_monitored_step(self, step: Step) -> Any:
            """Direct Coroutine Wrapper replacing monkey-patching completely."""
            if getattr(self, "closed", False) or getattr(self, "_failure_reason", None) is not None:
                return None
            
            clean_name = step.name.lstrip('/')
            is_valid = self._is_valid_cwl_step(clean_name)

            # if is_valid:
            #     _yprov_log(f"[STEP START] Launching execution for step: '{clean_name}'")

            start_time_ns = time.time_ns()
            self._attach_step_monitors(step)

            try:
                result = await step.run()
                end_time_ns = time.time_ns()
                
                if is_valid:
                    duration = (end_time_ns - start_time_ns) / 1e9
                    #_yprov_log(f"[STEP COMPLETE] Step '{clean_name}' finished in {duration:.2f}s")

                    if clean_name not in self.job_recorded_steps:
                        self._on_step_complete(step, start_time_ns, end_time_ns, status=Status.COMPLETED)

                return result
            
            except asyncio.CancelledError:
                raise
            
            except Exception as e:
                end_time_ns = time.time_ns()
                if is_valid:
                    _yprov_log(f"[STEP FAILED] Step '{clean_name}' failed: {e}", level="error")
                    if clean_name not in self.job_recorded_steps:
                        self._on_step_complete(step, start_time_ns, end_time_ns, status=Status.FAILED)
                raise

        def _on_step_complete(self, step: Step, start_time_ns: int, end_time_ns: int, status: Status = Status.COMPLETED):
            clean_name = step.name.lstrip('/')

            if not self._is_valid_cwl_step(clean_name):
                return

            task_id = str(uuid.uuid4())
            task = YProvTask(task_id, clean_name)
            task._start_time = sf_utils.get_date_from_ns(start_time_ns)
            task._end_time = sf_utils.get_date_from_ns(end_time_ns)
            task._status = _get_action_status(status)
            task._level = '1'

            # Extract Inputs
            in_ports = step.get_input_ports() if hasattr(step, 'get_input_ports') else getattr(step, 'input_ports', {})
            for port_name, port_obj in in_ports.items():
                port_label = port_name.split('/')[-1]
                if _is_system_spur(port_label) or _is_system_spur(port_name):
                    continue
                dt, dv, dl = _extract_memory_port_metadata(port_obj)
                data_id = _generate_entity_id(clean_name, port_label, dt, dv, dl, is_output=False)

                data_in = YProvData(data_id, port_label)
                data_in._type, data_in._value, data_in._location = dt, dv, dl
                task.add_input(data_in)
                data_in.add_consumer(task._id)

            # Extract Outputs
            out_ports = step.get_output_ports() if hasattr(step, 'get_output_ports') else getattr(step, 'output_ports', {})
            for port_name, port_obj in out_ports.items():
                port_label = port_name.split('/')[-1]
                if _is_system_spur(port_label) or _is_system_spur(port_name):
                    continue
                dt, dv, dl = _extract_memory_port_metadata(port_obj)
                data_id = _generate_entity_id(clean_name, port_label, dt, dv, dl, is_output=True)

                data_out = YProvData(data_id, port_label)
                data_out._type, data_out._value, data_out._location = dt, dv, dl
                task.add_output(data_out)
                data_out.set_producer(task._id)

            self.register_and_flush_task(clean_name, task)

        def register_and_flush_task(self, clean_name: str, task: YProvTask) -> None:
            if not self.prov_workflow:
                _yprov_log("Cannot flush data: prov_workflow is not initialized.", level="warning")
                return

            self.prov_workflow.add_task(task)
            
            if clean_name not in self.tasks_by_step_name:
                self.tasks_by_step_name[clean_name] = []
            self.tasks_by_step_name[clean_name].append(task)

            self._pending_since_flush += 1
            self._maybe_flush()

        def _maybe_flush(self) -> None:
            """
            Writes an intermediate snapshot after every _FLUSH_BATCH_SIZE
            captured tasks, or after _FLUSH_MIN_INTERVAL_S seconds since the
            last flush, whichever comes first. Each flush re-serializes and
            re-purges the WHOLE graph, so writing once per task would cost
            O(T^2) total for a T-task workflow; batching cuts that by roughly
            the batch size.

            Schedules _flush_prov_json_async() as a background task via
            asyncio.create_task() rather than blocking whatever just captured 
            a task while the write happens. This is called from both async
            (_on_job_complete's coroutine wrapper) and plain-sync
            (_attach_step_monitors' sync wrapper branch) call sites, so it
            can't safely `await` here itself; create_task() only needs a
            running event loop, not an async caller.

            At most one flush task is ever in flight at a time: if one is
            already running when a new batch fills, this just marks that
            another pass is needed (_flush_again) instead of spawning a
            second independent task, avoiding both a backlog of redundant
            writes and any chance of two writes to the same file completing
            out of order.

            run()'s own end-of-workflow and failure-path calls await
            _flush_prov_json_async() directly instead of going through this
            method, the FINAL file write is always waited-on to completion,
            never a fire-and-forget task that could get cut off by the
            process exiting.
            """
            now = time.time()
            should_flush = (
                self._pending_since_flush >= self._flush_batch_size
                or (now - self._last_flush_ts) >= self._flush_min_interval_s
            )
            if not should_flush:
                return None

            self._pending_since_flush = 0
            self._last_flush_ts = time.time()

            if self._flush_task is not None and not self._flush_task.done():
                self._flush_again = True
                return None

            try:
                self._flush_task = asyncio.create_task(self._run_flush_with_coalescing())
            except RuntimeError:
                self._flush_prov_json()

        async def _run_flush_with_coalescing(self) -> Optional[str]:
            """
            Runs one flush, then, if more tasks were captured while it was
            running, runs exactly one more, instead of letting a backlog of
            redundant flush tasks pile up. _flush_prov_json_async() itself
            still serializes against any OTHER in-flight flush via
            self._flush_lock: this method keeps the number of scheduled
            flush tasks at any moment to at most one, and the lock guarantees 
            that even if two ever coexisted, their writes could never complete 
            out of order.
            """
            result = await self._flush_prov_json_async()
            if self._flush_again:
                self._flush_again = False
                result = await self._flush_prov_json_async()
            return result

        def _compute_flush_payload(self) -> Optional[Tuple[str, MutableMapping[str, Any]]]:
            """
            Builds the purged/edge-injected provenance dict entirely in memory
            and returns (target_path, prov_data) WITHOUT touching disk.

            Uses to_prov() (the library's in-memory JSON-string builder)
            instead of prov_to_json() (which does its own blocking write to
            disk that we'd immediately overwrite anyway). That removes one
            full write + one full re-read per flush.
            """
            try:
                os.makedirs(self.outdir, exist_ok=True)
                prov_json_str = self.prov_workflow.to_prov()
                if not prov_json_str:
                    _yprov_log("yprov4wfs to_prov() returned no data.", level="error")
                    return None

                json_file_path = 'yprov4wfs.json'
                prov_data = json.loads(prov_json_str)

                # -----------------------------------------------------------
                # STRICT WHITELIST & ENTITY FILTERING
                # -----------------------------------------------------------
                def is_valid_cwl_activity(label: str) -> bool:
                    # Delegate to the single shared, memoized check used at
                    # attach-time
                    return self._is_valid_cwl_step(label)

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

                # Re-inject explicit step-to-step dependencies (wasInformedBy) between valid tasks
                prov_data["wasInformedBy"] = {} 
                existing_relations = set()

                def _inject_edge(p_uuid: str, c_uuid: str) -> None:
                    if p_uuid != c_uuid and (c_uuid, p_uuid) not in existing_relations:
                        if p_uuid in valid_activity_ids and c_uuid in valid_activity_ids:
                            rel_key = f"_:informed_{hashlib.md5(f'{c_uuid}{p_uuid}'.encode()).hexdigest()[:8]}"
                            prov_data["wasInformedBy"][rel_key] = {"prov:informed": c_uuid, "prov:informant": p_uuid}
                            existing_relations.add((c_uuid, p_uuid))

                def _resolve_leaf_tasks(step_name: str) -> List[Any]:
                    clean_s = step_name.lstrip('/')
                    exact_match = (
                        self.tasks_by_step_name.get(clean_s) 
                        or self.tasks_by_step_name.get(f"/{clean_s}")
                    )
                    if exact_match:
                        return [t for t in exact_match if t._id in valid_activity_ids]
                    
                    prefix = f"{clean_s}/"
                    child_tasks = []
                    for task_name, tasks in self.tasks_by_step_name.items():
                        normalized_name = task_name.lstrip('/')
                        if normalized_name.startswith(prefix):
                            child_tasks.extend([t for t in tasks if t._id in valid_activity_ids])
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
                            for c_task in child_tasks: _inject_edge(parent_tasks[0]._id, c_task._id)
                        elif c_len == 1 and p_len > 1:
                            for p_task in parent_tasks: _inject_edge(p_task._id, child_tasks[0]._id)
                        elif p_len == c_len:
                            for p_task, c_task in zip(parent_tasks, child_tasks): _inject_edge(p_task._id, c_task._id)
                        else:
                            for p_task in parent_tasks:
                                for c_task in child_tasks: _inject_edge(p_task._id, c_task._id)

                return json_file_path, prov_data

            except Exception as e:
                _yprov_log(f"JSON Flush Error (compute phase): {e}\n{traceback.format_exc()}", level="error")
                return None

        @staticmethod
        def _write_json_file_sync(json_file_path: str, prov_data: MutableMapping[str, Any]) -> int:
            """
            The actual blocking disk write, run off the event loop thread via
            asyncio.to_thread() (see _flush_prov_json_async). Kept as a plain
            @staticmethod rather than inline code so asyncio.to_thread has a
            single, simple, picklable-argument callable to dispatch.
            """
            with open(json_file_path, 'w') as f:
                json.dump(prov_data, f, indent=4)
            return os.path.getsize(json_file_path)

        async def _flush_prov_json_async(self) -> Optional[str]:
            """
            Computes the purged/edge-injected snapshot, then performs the 
            actual disk write via asyncio.to_thread() so the write itself 
            doesn't block the event loop while StreamFlow is scheduling 
            other steps.

            Wrapped in self._flush_lock so that even if this ever gets
            invoked from more than one place concurrently, the compute-then-
            write sequence is atomic end-to-end: payload computation happens
            AFTER acquiring the lock, so a flush that had to wait picks up
            the freshest state rather than a stale snapshot, and writes can
            never land on disk out of order. Without this, multiple
            fire-and-forget flush tasks dispatched via asyncio.to_thread's
            thread pool could have their writes to the SAME file complete out
            of order; an older, smaller snapshot finishing AFTER a newer,
            more complete one, silently clobbering it with less data even
            though nothing was actually lost from self.prov_workflow itself.
            """
            async with self._flush_lock:
                payload = self._compute_flush_payload()
                if payload is None:
                    return None
                json_file_path, prov_data = payload

                try:
                    file_size = await asyncio.to_thread(self._write_json_file_sync, json_file_path, prov_data)
                    #_yprov_log(f"[FLUSH SUCCESS] Updated JSON written to: {json_file_path} ({file_size} bytes)")
                    return json_file_path
                except Exception as e:
                    _yprov_log(f"JSON Flush Error (write phase): {e}\n{traceback.format_exc()}", level="error")
                    return None

        def _flush_prov_json(self) -> Optional[str]:
            """
            Synchronous convenience wrapper, computes the payload and writes
            it immediately, blocking. Used only as a fallback if no event
            loop is reachable from the calling context; the async path above
            is preferred everywhere else.
            """
            payload = self._compute_flush_payload()
            if payload is None:
                return None
            json_file_path, prov_data = payload
            try:
                self._write_json_file_sync(json_file_path, prov_data)
                return json_file_path
            except Exception as e:
                _yprov_log(f"JSON Flush Error (write phase): {e}\n{traceback.format_exc()}", level="error")
                return None

        def _parse_cwl_for_dependencies(self) -> MutableMapping[str, List[str]]:
            dependencies = {}
            streamflow_config_path = self.map_file.get("config")
            cwl_files = discover_workflow_cwl_files(streamflow_config_path)

            if not cwl_files:
                _yprov_log("No CWL files found to parse dependencies.", level="warning")
                return dependencies

            cwl_registry = {}
            for filename in cwl_files:
                try:
                    real_filename = os.path.abspath(os.path.realpath(filename))
                    with open(real_filename, 'r') as f:
                        data = yaml.safe_load(f)
                        if data:
                            cwl_registry[os.path.basename(filename)] = data
                except Exception as e:
                    _yprov_log(f"Error reading CWL file {filename}: {e}", level="warning")
                    continue

            def extract_steps_recursive(workflow_data, current_prefix=""):
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
                            extract_steps_recursive(run_target, current_prefix=next_prefix)
                        elif isinstance(run_target, str):
                            target_filename = os.path.basename(run_target)
                            if target_filename in cwl_registry:
                                extract_steps_recursive(cwl_registry[target_filename], current_prefix=next_prefix)
                                
            main_workflow_file = None
            if streamflow_config_path and os.path.exists(streamflow_config_path):
                try:
                    with open(streamflow_config_path, 'r') as sf:
                        sf_data = yaml.safe_load(sf)
                    workflows_sec = sf_data.get('workflows', {})
                    for wf_name, wf_val in workflows_sec.items():
                        wf_config = wf_val.get('config', {})
                        wf_file_path = wf_config.get('file')
                        if wf_file_path:
                            main_workflow_file = os.path.basename(wf_file_path)
                            _yprov_log(f"Extracted root workflow file from streamflow's yml: {main_workflow_file}")
                            break
                except Exception as e:
                    _yprov_log(f"Failed reading streamflow.yml: {e}", level="warning")

            if not main_workflow_file:
                for base_name, data in cwl_registry.items():
                    if data.get('class') == 'Workflow':
                        main_workflow_file = base_name
                        break

            if main_workflow_file:
                _yprov_log(f"Parsing CWL hierarchy starting at root: {main_workflow_file}")
                extract_steps_recursive(cwl_registry[main_workflow_file], current_prefix="")

            _yprov_log(f"Computed CWL Dependency Graph: {dependencies}")
            return dependencies

        async def _handle_exception(self, task: asyncio.Task):
            try:
                return await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                if getattr(self, "_failure_reason", None) is None:
                    self._failure_reason = exc
                if not self.closed:
                    await self._shutdown()

        async def _shutdown(self):
            if self.closed:
                return
            self.closed = True  # Block concurrent calls immediately

            # Cancel all background task executions BEFORE tearing down connectors
            current_task = asyncio.current_task()
            pending_tasks = []
            
            if hasattr(self, "executions") and self.executions:
                for task in self.executions:
                    if task is not current_task and not task.done():
                        task.cancel()
                        pending_tasks.append(task)

            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

            # Terminate remaining steps cleanly
            await asyncio.gather(
                *(
                    asyncio.create_task(step.terminate(Status.CANCELLED))
                    for step in self.workflow.steps.values()
                    if not step.terminated
                ),
                return_exceptions=True,
            )

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
            Executes the workflow graph with native step monitoring and progressive JSON flushing.
            """
            try:
                output_tokens = {}
                _yprov_log(f"Starting execution loop for Workflow ID: {self.workflow.persistent_id}")
                #_yprov_log(f"Discovered total steps in workflow object: {len(self.workflow.steps)}")
                
                start_time_root_ns = time.time_ns()
                await self.workflow.context.database.update_workflow(
                    self.workflow.persistent_id, {"start_time": start_time_root_ns}
                )

                # Initialize master workflow wrapper & parse CWL dependencies upfront
                self.prov_workflow = YProvWorkflow(self.workflow.name, f'workflow_{self.workflow.name}')
                self.prov_workflow._start_time = sf_utils.get_date_from_ns(start_time_root_ns)
                self.prov_workflow._engineWMS = 'StreamFlow'
                self.prov_workflow._level = '0'
                if "config" in self.map_file: 
                    self.prov_workflow._resource_cwl_uri = self.map_file["config"]

                self.computed_cwl_deps = self._parse_cwl_for_dependencies()

                # Schedule ALL workflow steps to avoid DAG token deadlocks
                for task_name, step in self.workflow.steps.items():
                    clean_name = step.name.lstrip('/')

                    if self._is_valid_cwl_step(clean_name):
                        self._attach_step_monitors(step)
                        #_yprov_log(f"Scheduling step '{step.name}' via _run_monitored_step")

                    execution = asyncio.create_task(
                        self._handle_exception(asyncio.create_task(self._run_monitored_step(step))),
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
                    #_yprov_log("Synchronizing background steps...")
                    done, pending = await asyncio.wait(self.executions, timeout=3.0)
                    if pending:
                        _yprov_log(f"{len(pending)} steps did not join within timeout.", level="warning")

                for step in self.workflow.steps.values():
                    if step.status in [Status.FAILED, Status.CANCELLED]:
                        reason = getattr(self, "_failure_reason", None)
                        if reason is not None:
                            raise WorkflowExecutionException(f"FAILED Workflow execution: {reason}") from reason
                        raise WorkflowExecutionException("FAILED Workflow execution")

                end_time_root_ns = time.time_ns()
                self.prov_workflow._end_time = sf_utils.get_date_from_ns(end_time_root_ns)
                self.prov_workflow._status = _get_action_status(Status.COMPLETED)

                if self.workflow.persistent_id:
                    await self.workflow.context.database.update_workflow(
                        self.workflow.persistent_id,
                        {"status": Status.COMPLETED.value, "end_time": end_time_root_ns},
                    )
                
                # Final flush & package
                json_file_path = await self._flush_prov_json_async()

                if json_file_path and os.path.exists(json_file_path):
                    path = os.path.join(self.outdir, self.workflow.name + ".zip")
                    with ZipFile(path, "w") as archive:
                        archive.write(json_file_path, arcname="provenance.json")  
                        for src, dst in self.map_file.items():
                            if os.path.exists(src) and dst not in archive.namelist():
                                archive.write(src, dst)
                    
                    _yprov_log(f"Successfully generated final zip package at: {path}")
                
                try:
                    import concurrent.futures.process
                    atexit.unregister(concurrent.futures.process._python_exit)
                except Exception:
                    pass

                _yprov_log("Execution finished successfully. Returning output tokens.")
                
                import threading
                def delayed_exit():
                    time.sleep(1.0)
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os._exit(0)
                
                threading.Thread(target=delayed_exit, daemon=True).start()
                return output_tokens

            except WorkflowExecutionException as e:
                reason = getattr(self, "_failure_reason", e)
                if self.prov_workflow:
                    self.prov_workflow._end_time = sf_utils.get_date_from_ns(time.time_ns())
                    self.prov_workflow._status = _get_action_status(Status.FAILED)
                    await self._flush_prov_json_async()

                if self.workflow.persistent_id:
                    await self.workflow.context.database.update_workflow(
                        self.workflow.persistent_id,
                        {"status": Status.FAILED.value, "end_time": time.time_ns()},
                    )
                if not self.closed:
                    await self._shutdown()

                # Clean error summary banner output
                logger.error("\n" + "=" * 62)
                logger.error(" WORKFLOW EXECUTION FAILED")
                logger.error(f" Reason: {reason}")
                logger.error(" Provenance: yprov4wfs.json updated and saved successfully.")
                logger.error("=" * 62 + "\n")
                
                # Flush standard I/O streams and hard-exit cleanly
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(1)

            except Exception as e:
                _yprov_log(f"Unexpected Executor Error: {e}", level="error")
                
                if self.prov_workflow:
                    self.prov_workflow._end_time = sf_utils.get_date_from_ns(time.time_ns())
                    self.prov_workflow._status = _get_action_status(Status.FAILED)
                    await self._flush_prov_json_async()

                if self.workflow.persistent_id:
                    await self.workflow.context.database.update_workflow(
                        self.workflow.persistent_id,
                        {"status": Status.FAILED.value, "end_time": time.time_ns()},
                    )
                if not self.closed:
                    await self._shutdown()

                logger.error("\n" + "=" * 62)
                logger.error(" WORKFLOW EXECUTION FAILED")
                logger.error(f" Reason: {e}")
                logger.error(" Provenance: yprov4wfs.json updated and saved successfully.")
                logger.error("=" * 62 + "\n")
                
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(1)


        async def close(self) -> None:
            if self._closed:
                return
            if self._closing is not None:
                await self._closing.wait()
            else:
                # Terminate all steps
                await asyncio.gather(
                    *(
                        asyncio.create_task(step.terminate(Status.CANCELLED))
                        for step in self.workflow.steps.values()
                        if not step.terminated
                    )
                )
                # Mark the executor as closed
                self._closed = True

        async def closed(self) -> bool:
            if self._closing is not None:
                await self._closing.wait()
            return self._closed

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
            self._closed: bool = False
            self._closing: asyncio.Event | None = None
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
            
        async def close(self) -> None:
            if self._closed:
                return
            if self._closing is not None:
                await self._closing.wait()
            else:
                # Terminate all steps
                await asyncio.gather(
                    *(
                        asyncio.create_task(step.terminate(Status.CANCELLED))
                        for step in self.workflow.steps.values()
                        if not step.terminated
                    )
                )
                # Mark the executor as closed
                self._closed = True

        async def closed(self) -> bool:
            if self._closing is not None:
                await self._closing.wait()
            return self._closed