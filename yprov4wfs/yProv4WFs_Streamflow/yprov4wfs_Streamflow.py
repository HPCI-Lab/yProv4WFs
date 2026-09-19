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
import threading
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
import re
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

# Batch size check
try:
    _FLUSH_BATCH_SIZE = int(os.getenv("BATCH_SIZE", 10)) # tasks
    if _FLUSH_BATCH_SIZE <= 0: # Negative values are NOT valid, fallback value
        _FLUSH_BATCH_SIZE = 10
except ValueError:
    _FLUSH_BATCH_SIZE = 10  # Fallback if BATCH_SIZE is set to non-numeric text

_FLUSH_MIN_INTERVAL_S = 5.0 * 60.0 # minutes

# -------------------------------------------------------
# FILE FINGERPRINTING & ARTIFACT REGISTRY
# -------------------------------------------------------
# Maps SHA-256 file hash -> producing_task_id
HASH_REGISTRY: MutableMapping[str, str] = {}

def compute_file_hash(path: Optional[str], chunk_size: int = 65536) -> Optional[str]:
    """
    Computes SHA-256 hash of a file safely without loading the full file into memory.
    Operating system and workflow engine agnostic.
    """
    if not path or not isinstance(path, str) or path == "None" or not os.path.isfile(path):
        return None
    hasher = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)
        return f"sha256:{hasher.hexdigest()}"
    except (OSError, IOError):
        return None

def _yprov_log(msg: str, level: str = "info"):
    """Guaranteed unbuffered console log helper."""
    log_func = getattr(logger, level.lower(), logger.info)
    log_func(f"[YPROV ONLINE PLUGIN] {msg}")

def _is_system_spur(name: str) -> bool:
    """Check if a step, port, or entity is an internal StreamFlow runtime component."""
    clean = name.lstrip('/').lower()
    if "__" in clean:
        return True
    
    system_keywords = {
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
    }
    segments = clean.split('/')
    return any(seg in system_keywords for seg in segments)

def _get_scatter_tag(task: Any) -> Optional[Tuple[int, ...]]:
    """
    Extracts the FULL hierarchical scatter tag from a task's real Job name, as a
    tuple of ints -- e.g. job name '/band/mProject/0.1.0' -> (0, 1, 0).

    This is a nested tag, not a flat index: its depth (number of components)
    tracks scatter nesting. A depth-1 tag ('0') means "not scattered" (e.g.
    get_rgb, color_image). A depth-2 tag ('0.1') means "scattered once" (e.g.
    per-band steps like extract_bid, mConcatFit). A depth-3 tag ('0.1.0') means
    "scattered twice" (e.g. per-band-per-tile steps like mProject, mBackground).

    Two tasks belong to the same band/tile group -- and should be connected --
    exactly when one tag is a prefix of the other, regardless of whether their
    depths (or raw per-step counts) match. Comparing only the trailing int
    (the old _get_scatter_index) throws away which band/tile that int belongs
    to and is NOT sufficient for grouping mismatched-depth pairs correctly.
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

def _tags_are_compatible(parent_tag: str, child_tag: str) -> bool:
    """
    Check if raw job-name tags match exactly or share a parent-child iteration
    prefix. Mirrors _tags_share_lineage but works directly on the raw string
    (permissive when either side is missing/empty), so it still catches lineage
    even when _get_scatter_tag can't parse a tag as pure ints. Ported verbatim
    from the offline (fromdb) plugin to keep both plugins' matching decisions
    identical.
    """
    if not parent_tag or not child_tag:
        return True
    if parent_tag == child_tag:
        return True

    p_parts = str(parent_tag).strip(".").split(".")
    c_parts = str(child_tag).strip(".").split(".")
    min_len = min(len(p_parts), len(c_parts))

    return p_parts[:min_len] == c_parts[:min_len]

def _get_scatter_index(task: Any) -> Optional[int]:
    """
    Extracts scatter index or iteration number from a task object or identifier.
    Supports patterns like: step_name_0, step_name_job_0, step_name[0], step_name/0

    Kept only as a coarse display/legacy helper (used in diagnostic logging).
    Real edge-matching now uses _get_scatter_tag() + _tags_share_lineage(),
    since a single trailing int can't express hierarchical band/tile lineage.

    Prefers `task._job_name` (the real Job name StreamFlow assigned this specific
    scatter iteration, stamped at task-creation time). `task._id` is a random
    UUID and `task._name`/`task.name` is the shared step name -- neither one has
    ever carried a real index, so they're kept only as a last-resort fallback for
    tasks created before this field existed.
    """
    t_job = str(getattr(task, '_job_name', '') or '')
    t_id = str(getattr(task, '_id', '') or getattr(task, 'id', '') or '')
    t_name = str(getattr(task, '_name', '') or getattr(task, 'name', '') or '')

    for candidate in (t_job, t_id, t_name):
        if not candidate:
            continue
        # Bracket notation: step[0]
        bracket_match = re.search(r'\[(\d+)\]', candidate)
        if bracket_match:
            return int(bracket_match.group(1))
        # Suffix notation: step_0, step_job_0, step/0, step-0
        suffix_match = re.search(r'[\/_#-](?:job[_\-]?)?(\d+)$', candidate)
        if suffix_match:
            return int(suffix_match.group(1))
            
    return None

def _generate_entity_id(
    step_name: str, 
    port_label: str, 
    p_type: str, 
    p_val: str, 
    p_loc: str, 
    is_output: bool = False,
    file_hash: Optional[str] = None
) -> str:
    """
    Generate deterministic Entity IDs strictly scoped to data content or step/port.
    If a SHA-256 content hash is available, uses the hash to ensure exact content-addressable matching.
    """
    clean_step = step_name.lstrip('/')

    # Content-addressable file entity (Decoupled from Streamflow internal paths)
    if file_hash:
        clean_hash = file_hash.replace("sha256:", "")[:16]
        return f"ent_file_{clean_hash}"

    # File or directory entity fallback with valid path/location
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
        
        result = sorted(list(discovered_files))
        _yprov_log(f"Discovered CWL target files: {result}")
        return result

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
            self._total_flush_count = 0 # total number of flushes done
            # Dynamically intercept the target .yml / .yaml file from the run command
            yaml_args = [arg for arg in sys.argv if arg.endswith(('.yml', '.yaml'))]
            self.streamflow_config_path = yaml_args[0] if yaml_args else "streamflow.yml"
            
            self.map_file["config"] = self.streamflow_config_path
            self.outdir = "./outputs"

            # Memoizes _is_valid_cwl_step() per step name
            self._activity_validity_cache: MutableMapping[str, bool] = {}

            self._flush_batch_size = _FLUSH_BATCH_SIZE
            self._flush_min_interval_s = _FLUSH_MIN_INTERVAL_S
            self._pending_since_flush = 0
            self._last_flush_ts = 0.0

            self._task_registration_lock = threading.Lock()
            self._flush_lock = asyncio.Lock()
            self._flush_task: Optional[asyncio.Task] = None
            self._flush_again = False

        def _is_valid_cwl_step(self, clean_name: str) -> bool:
            cached = self._activity_validity_cache.get(clean_name)
            if cached is not None:
                return cached

            result = self._compute_is_valid_cwl_step(clean_name)
            self._activity_validity_cache[clean_name] = result
            return result

        def _compute_is_valid_cwl_step(self, clean_name: str) -> bool:
            if _is_system_spur(clean_name):
                return False

            if not self.computed_cwl_deps:
                return True

            parts = clean_name.lstrip('/').split('/')
            cleaned_parts = [re.sub(r'(_\d+|\[\d+\])$', '', p) for p in parts]
            normalized_path = "/" + "/".join(cleaned_parts)
            base_step_name = cleaned_parts[-1]

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
                                job_obj = kwargs.get('job') or (args[0] if args else None)
                                try:
                                    res = await orig_method(*args, **kwargs)
                                    end_ns = time.time_ns()
                                    actual_status = getattr(step, "status", Status.COMPLETED)
                                    self._on_job_complete(step, start_ns, end_ns, status=actual_status, job=job_obj)
                                    return res
                                except Exception:
                                    end_ns = time.time_ns()
                                    self._on_job_complete(step, start_ns, end_ns, status=Status.FAILED, job=job_obj)
                                    raise
                            wrapper._is_yprov_hook = True
                            setattr(step, method_name, wrapper)
                        else:
                            def wrapper(*args, **kwargs):
                                start_ns = time.time_ns()
                                job_obj = kwargs.get('job') or (args[0] if args else None)
                                try:
                                    res = orig_method(*args, **kwargs)
                                    end_ns = time.time_ns()
                                    actual_status = getattr(step, "status", Status.COMPLETED)
                                    self._on_job_complete(step, start_ns, end_ns, status=actual_status, job=job_obj)
                                    return res
                                except Exception:
                                    end_ns = time.time_ns()
                                    self._on_job_complete(step, start_ns, end_ns, status=Status.FAILED, job=job_obj)
                                    raise
                            wrapper._is_yprov_hook = True
                            setattr(step, method_name, wrapper)

        def _create_task_from_step(
            self, 
            step: Step, 
            start_time_ns: int, 
            end_time_ns: int, 
            status: Status = Status.COMPLETED,
            job: Any = None
        ) -> Optional[YProvTask]:
            """
            Builds a YProvTask with incremental SHA-256 fingerprinting for input and output files.
            """
            clean_name = step.name.lstrip('/')
            if not self._is_valid_cwl_step(clean_name):
                return None

            if status in (Status.CANCELLED, Status.SKIPPED):
                _yprov_log(f"Skipping provenance record for '{clean_name}': status={status}", level="debug")
                return None

            task_id = str(uuid.uuid4())
            task = YProvTask(task_id, clean_name)
            task._start_time = sf_utils.get_date_from_ns(start_time_ns)
            task._end_time = sf_utils.get_date_from_ns(end_time_ns)
            task._status = _get_action_status(status)
            task._level = '1'

            job_name = getattr(job, 'name', None) or getattr(job, 'get_name', lambda: None)()
            if job_name:
                task._job_name = str(job_name)
                if not hasattr(self, '_logged_job_name_steps'):
                    self._logged_job_name_steps = set()
                if clean_name not in self._logged_job_name_steps:
                    self._logged_job_name_steps.add(clean_name)
            else:
                task._job_name = None

            # Extract Inputs with SHA-256 Fingerprinting
            in_ports = step.get_input_ports() if hasattr(step, 'get_input_ports') else getattr(step, 'input_ports', {})
            for port_name, port_obj in in_ports.items():
                port_label = port_name.split('/')[-1]
                if _is_system_spur(port_label) or _is_system_spur(port_name):
                    continue
                dt, dv, dl = _extract_memory_port_metadata(port_obj)
                
                # Compute SHA-256 hash for file artifacts on disk
                file_hash = compute_file_hash(dl) if dl and dl != "None" else None
                data_id = _generate_entity_id(clean_name, port_label, dt, dv, dl, is_output=False, file_hash=file_hash)
                
                data_in = YProvData(data_id, port_label)
                data_in._type, data_in._value, data_in._location = dt, dv, dl
                task.add_input(data_in)
                data_in.add_consumer(task._id)

            # Extract Outputs with SHA-256 Fingerprinting & Registry Mapping
            out_ports = step.get_output_ports() if hasattr(step, 'get_output_ports') else getattr(step, 'output_ports', {})
            for port_name, port_obj in out_ports.items():
                port_label = port_name.split('/')[-1]
                if _is_system_spur(port_label) or _is_system_spur(port_name):
                    continue
                dt, dv, dl = _extract_memory_port_metadata(port_obj)
                
                # Compute SHA-256 hash for file artifacts on disk
                file_hash = compute_file_hash(dl) if dl and dl != "None" else None
                data_id = _generate_entity_id(clean_name, port_label, dt, dv, dl, is_output=True, file_hash=file_hash)

                data_out = YProvData(data_id, port_label)
                data_out._type, data_out._value, data_out._location = dt, dv, dl
                task.add_output(data_out)
                data_out.set_producer(task._id)

                # Store file fingerprint in global registry
                if file_hash:
                    HASH_REGISTRY[file_hash] = task._id

            return task

        def _on_job_complete(self, step: Step, start_time_ns: int, end_time_ns: int, status: Status = Status.COMPLETED, job: Any = None):
            clean_name = step.name.lstrip('/')
            task = self._create_task_from_step(step, start_time_ns, end_time_ns, status, job=job)
            if task:
                self.job_recorded_steps.add(clean_name)
                self.register_and_flush_task(clean_name, task)

        def _on_step_complete(self, step: Step, start_time_ns: int, end_time_ns: int, status: Status = Status.COMPLETED):
            clean_name = step.name.lstrip('/')
            task = self._create_task_from_step(step, start_time_ns, end_time_ns, status)
            if task:
                self.register_and_flush_task(clean_name, task)

        async def _run_monitored_step(self, step: Step) -> Any:
            """Direct Coroutine Wrapper replacing monkey-patching completely."""
            if getattr(self, "closed", False) or getattr(self, "_failure_reason", None) is not None:
                return None
            
            clean_name = step.name.lstrip('/')
            is_valid = self._is_valid_cwl_step(clean_name)

            start_time_ns = time.time_ns()
            self._attach_step_monitors(step)

            try:
                result = await step.run()
                end_time_ns = time.time_ns()
                
                if is_valid:
                    if clean_name not in self.job_recorded_steps:
                        actual_status = getattr(step, "status", Status.COMPLETED)
                        self._on_step_complete(step, start_time_ns, end_time_ns, status=actual_status)

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

        def register_and_flush_task(self, clean_name: str, task: YProvTask) -> None:
            if not self.prov_workflow:
                _yprov_log("Cannot flush data: prov_workflow is not initialized.", level="warning")
                return
        
            with self._task_registration_lock:
                self.prov_workflow.add_task(task)
                if clean_name not in self.tasks_by_step_name:
                    self.tasks_by_step_name[clean_name] = []
                self.tasks_by_step_name[clean_name].append(task)

            self._pending_since_flush += 1
            self._maybe_flush()

        def _maybe_flush(self) -> None:
            now = time.time()
            should_flush = (
                self._pending_since_flush >= self._flush_batch_size
                or (now - self._last_flush_ts) >= self._flush_min_interval_s
            )
            if not should_flush:
                return None

            self._pending_since_flush = 0
            self._last_flush_ts = time.time()

            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is self._loop:
                self._schedule_flush()
            else:
                try:
                    self._loop.call_soon_threadsafe(self._schedule_flush)
                except RuntimeError:
                    _yprov_log(
                        "Dropped a late flush trigger: event loop already closed.",
                        level="warning",
                    )

        def _schedule_flush(self) -> None:
            if self._flush_task is not None and not self._flush_task.done():
                self._flush_again = True
                return
            self._flush_task = self._loop.create_task(self._run_flush_with_coalescing())

        async def _run_flush_with_coalescing(self) -> Optional[str]:
            result = await self._flush_prov_json_async()
            if self._flush_again:
                self._flush_again = False
                result = await self._flush_prov_json_async()
            return result

        def _compute_flush_payload(self) -> Optional[Tuple[str, MutableMapping[str, Any]]]:
            """
            Builds the purged/edge-injected provenance dict entirely in memory
            and returns (target_path, prov_data) WITHOUT touching disk.
            Includes deep diagnostic logging for wasInformedBy generation.
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

                # -----------------------------------------------------------
                # EDGE INJECTION DIAGNOSTICS & RESOLUTION
                # -----------------------------------------------------------
                prov_data["wasInformedBy"] = {}
                existing_relations = set()
                edge_stats_by_pair = {}
                def _inject_edge(p_uuid: str, c_uuid: str, reason: str = "") -> None:
                    if p_uuid == c_uuid:
                        # reject self-loop edges (task informing itself) as invalid
                        return

                    if (c_uuid, p_uuid) in existing_relations:
                        # skipped duplicate edges (already injected) to avoid redundant entries in wasInformedBy
                        return

                    if p_uuid in valid_activity_ids and c_uuid in valid_activity_ids:
                        # injet edge only if both parent and child are valid CWL activities
                        rel_key = f"_:informed_{hashlib.md5(f'{c_uuid}{p_uuid}'.encode()).hexdigest()[:8]}"
                        prov_data["wasInformedBy"][rel_key] = {"prov:informed": c_uuid, "prov:informant": p_uuid}
                        existing_relations.add((c_uuid, p_uuid))
                    #else: 
                        # invalid activity UUIDs detected

                all_cwl_paths = set(self.computed_cwl_deps.keys())
                for parent_list in self.computed_cwl_deps.values():
                    all_cwl_paths.update(parent_list)

                def is_composite(path: str) -> bool:
                    clean_p = path.lstrip('/')
                    prefix = f"{clean_p}/"
                    return any(p.lstrip('/').startswith(prefix) for p in all_cwl_paths if p != path)

                def get_leaf_descendants(path: str) -> List[str]:
                    clean_p = path.lstrip('/')
                    if not is_composite(path):
                        return [path]
                    prefix = f"{clean_p}/"
                    leaves = [
                        p for p in all_cwl_paths 
                        if p.lstrip('/').startswith(prefix) and not is_composite(p)
                    ]
                    return leaves if leaves else [path]

                def get_entry_leaves(path: str) -> List[str]:
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

                def _get_exact_leaf_tasks(step_name: str) -> List[Any]:
                    clean_s = step_name.lstrip('/')
                    exact_match = (
                        self.tasks_by_step_name.get(clean_s) 
                        or self.tasks_by_step_name.get(f"/{clean_s}")
                    )
                    found_tasks = []
                    if exact_match:
                        found_tasks = [t for t in exact_match if t._id in valid_activity_ids]
                    else:
                        prefix = f"{clean_s}/"
                        for task_name, tasks in self.tasks_by_step_name.items():
                            normalized_name = task_name.lstrip('/')
                            if normalized_name.startswith(prefix):
                                found_tasks.extend([t for t in tasks if t._id in valid_activity_ids])

                    return found_tasks

                def _get_task_sort_key(task: Any) -> Tuple[Tuple[str, ...], Tuple[str, ...], str]:
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
                    """
                    Content-based identity for a task's ports: real file location or
                    real scalar value. Ported verbatim from the offline (fromdb)
                    plugin -- used as the layer between lineage-tag matching and the
                    last-resort cross-product fallback, so both plugins fall back
                    identically when lineage tags aren't available for a pair.
                    """
                    items = getattr(task, 'outputs' if is_output else 'inputs',
                                     getattr(task, '_outputs' if is_output else '_inputs', []))
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

                for child_path, parent_paths in self.computed_cwl_deps.items():
                    child_entries = get_entry_leaves(child_path)

                    for parent_path in parent_paths:
                        parent_exits = get_exit_leaves(parent_path)
                        pair_key = f"{parent_path} -> {child_path}"

                        for p_leaf in parent_exits:
                            parent_tasks = _get_exact_leaf_tasks(p_leaf)
                            if not parent_tasks:
                                # no parent tasks found for this leaf, skip to next parent
                                continue

                            for c_leaf in child_entries:
                                child_tasks = _get_exact_leaf_tasks(c_leaf)
                                if not child_tasks:
                                    # no child tasks found for this leaf, skip to next child
                                    continue

                                p_len, c_len = len(parent_tasks), len(child_tasks)
                                before_count = len(prov_data["wasInformedBy"])

                                # 1. Broadcast (1 -> N): Single parent feeds all child instances
                                if p_len == 1 and c_len > 1:
                                    # routing pattern: single parent task informs multiple child tasks (broadcast)
                                    for c_task in child_tasks:
                                        _inject_edge(parent_tasks[0]._id, c_task._id, reason="Broadcast 1->N")

                                # 2. Gather (N -> 1): All parent instances feed single child
                                elif c_len == 1 and p_len > 1:
                                    # routing pattern: multiple parent tasks inform single child task (gather)
                                    for p_task in parent_tasks:
                                        _inject_edge(p_task._id, child_tasks[0]._id, reason="Gather N->1")

                                # 3. Single Task Link (1 -> 1)
                                elif p_len == 1 and c_len == 1:
                                    # routing pattern: single parent task informs single child task (1-to-1)
                                    _inject_edge(parent_tasks[0]._id, child_tasks[0]._id, reason="Single 1->1")

                                # 4. Parallel Scatter (N -> M): match by shared band/tile lineage
                                else:
                                    # routing pattern: multiple parent tasks inform multiple child tasks (parallel scatter)

                                    p_tags = {id(pt): _get_scatter_tag(pt) for pt in parent_tasks}
                                    c_tags = {id(ct): _get_scatter_tag(ct) for ct in child_tasks}

                                    if all(p_tags.values()) and all(c_tags.values()):
                                        # Real hierarchical lineage available on every task on both
                                        # sides. Connect any (parent, child) pair whose tags share a
                                        # prefix relationship (int-tuple check), OR whose raw job-name
                                        # tags are compatible (string-prefix check, permissive when a
                                        # tag is missing) -- same combined condition the offline
                                        # plugin uses, so both plugins make identical decisions here.
                                        for ct in child_tasks:
                                            c_tag = c_tags[id(ct)]
                                            matched = False
                                            for pt in parent_tasks:
                                                p_tag = p_tags[id(pt)]
                                                if _tags_share_lineage(p_tag, c_tag) or _tags_are_compatible(getattr(pt, '_job_name', ''), getattr(ct, '_job_name', '')):
                                                    _inject_edge(pt._id, ct._id, reason=f"Lineage Match (parent={p_tag}, child={c_tag})")
                                                    matched = True
                                            if not matched:
                                                # No parent tag matched this child tag; log a warning for diagnostic purposes
                                                pass

                                    # Fallback for unindexed steps of identical length: match 1:1 by sort key
                                    elif p_len == c_len:
                                        # Sort both parent and child task lists by a deterministic key (inputs, outputs, ID) to ensure consistent ordering
                                        sorted_parents = sorted(parent_tasks, key=_get_task_sort_key)
                                        sorted_children = sorted(child_tasks, key=_get_task_sort_key)
                                        for p_task, c_task in zip(sorted_parents, sorted_children):
                                            _inject_edge(p_task._id, c_task._id, reason="Sorted Key Fallback N->N")

                                    # No usable lineage tags AND mismatched counts: same layered
                                    # fallback as the offline plugin -- try content-based (real
                                    # location/value) matching first, and only cross-product as an
                                    # absolute last resort, still gated by tag compatibility.
                                    else:
                                        matched_any = False
                                        for p_task in parent_tasks:
                                            p_keys = _get_entity_keys(p_task, is_output=True)
                                            if not p_keys:
                                                continue
                                            for c_task in child_tasks:
                                                c_keys = _get_entity_keys(c_task, is_output=False)
                                                if p_keys & c_keys:
                                                    _inject_edge(p_task._id, c_task._id, reason="Content Match (loc/val)")
                                                    matched_any = True

                                        if not matched_any:
                                            for p_task in parent_tasks:
                                                for c_task in child_tasks:
                                                    if _tags_are_compatible(getattr(p_task, '_job_name', ''), getattr(c_task, '_job_name', '')):
                                                        _inject_edge(p_task._id, c_task._id, reason="Compatibility-Gated Cross Product")

                                added_edges = len(prov_data["wasInformedBy"]) - before_count
                                edge_stats_by_pair[pair_key] = edge_stats_by_pair.get(pair_key, 0) + added_edges

                return json_file_path, prov_data

            except Exception as e:
                _yprov_log(f"JSON Flush Error (compute phase): {e}\n{traceback.format_exc()}", level="error")
                return None

        @staticmethod
        def _write_json_file_sync(json_file_path: str, prov_data: MutableMapping[str, Any]) -> int:
            tmp_path = f"{json_file_path}.tmp.{os.getpid()}"
            with open(tmp_path, 'w') as f:
                json.dump(prov_data, f, indent=4)
            os.replace(tmp_path, json_file_path)
            return os.path.getsize(json_file_path)

        async def _flush_prov_json_async(self) -> Optional[str]:
            async with self._flush_lock:
                t_payload = time.perf_counter()
                payload = self._compute_flush_payload()
                if payload is None:
                    return None
                json_file_path, prov_data = payload
                payload_dur = time.perf_counter() - t_payload

                try:
                    flush_start_ts = time.perf_counter()
                    file_size = await asyncio.to_thread(self._write_json_file_sync, json_file_path, prov_data)
                    flush_duration_s = time.perf_counter() - flush_start_ts
                    
                    _yprov_log(f"[PROV_FLUSH] activities={len(prov_data.get('activity', {}))} "
                            f"entities={len(prov_data.get('entity', {}))} "
                            f"wasInformedBy={len(prov_data.get('wasInformedBy', {}))} "
                            f"file_size_bytes={file_size} "
                            f"payload_build_s={payload_dur:.4f} "
                            f"flush_write_s={flush_duration_s:.4f}")

                    self._total_flush_count += 1

                    return json_file_path
                except Exception as e:
                    _yprov_log(f"JSON Flush Error (write phase): {e}\n{traceback.format_exc()}", level="error")
                    return None

        def _flush_prov_json(self) -> Optional[str]:
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
                            cwl_registry[real_filename] = data
                except Exception as e:
                    _yprov_log(f"Error reading CWL file {filename}: {e}", level="warning")
                    continue

            def extract_steps_recursive(workflow_data, current_prefix="", current_file_path=None):
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
                            extract_steps_recursive(run_target, current_prefix=next_prefix, current_file_path=current_file_path)
                        elif isinstance(run_target, str):
                            clean_path = unquote(urlparse(run_target).path)
                            target_real_path = None
                            if current_file_path:
                                base_dir = os.path.dirname(current_file_path)
                                target_real_path = os.path.abspath(os.path.realpath(os.path.join(base_dir, clean_path)))
                            
                            if target_real_path and target_real_path in cwl_registry:
                                extract_steps_recursive(cwl_registry[target_real_path], current_prefix=next_prefix, current_file_path=target_real_path)
                            else:
                                target_filename = os.path.basename(run_target)
                                for r_path, r_data in cwl_registry.items():
                                    if os.path.basename(r_path) == target_filename and r_data.get('class') == 'Workflow':
                                        extract_steps_recursive(r_data, current_prefix=next_prefix, current_file_path=r_path)
                                        break
                                
            main_workflow_file = None
            main_workflow_path = None
            if streamflow_config_path and os.path.exists(streamflow_config_path):
                try:
                    real_config = os.path.abspath(os.path.realpath(streamflow_config_path))
                    with open(real_config, 'r') as sf:
                        sf_data = yaml.safe_load(sf)
                    workflows_sec = sf_data.get('workflows', {})
                    for wf_name, wf_val in workflows_sec.items():
                        wf_config = wf_val.get('config', {})
                        wf_file_path = wf_config.get('file')
                        if wf_file_path:
                            config_dir = os.path.dirname(real_config)
                            main_workflow_path = os.path.abspath(os.path.realpath(os.path.join(config_dir, wf_file_path)))
                            main_workflow_file = os.path.basename(wf_file_path)
                            _yprov_log(f"Extracted root workflow file from streamflow's yml: {main_workflow_file}")
                            break
                except Exception as e:
                    _yprov_log(f"Failed reading streamflow.yml: {e}", level="warning")

            if not main_workflow_path or main_workflow_path not in cwl_registry:
                for r_path, data in cwl_registry.items():
                    if data.get('class') == 'Workflow':
                        main_workflow_path = r_path
                        main_workflow_file = os.path.basename(r_path)
                        break

            if main_workflow_path and main_workflow_path in cwl_registry:
                _yprov_log(f"Parsing CWL hierarchy starting at root: {main_workflow_file}")
                extract_steps_recursive(cwl_registry[main_workflow_path], current_prefix="", current_file_path=main_workflow_path)

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
            self.closed = True

            current_task = asyncio.current_task()
            pending_tasks = []
            
            if hasattr(self, "executions") and self.executions:
                for task in self.executions:
                    if task is not current_task and not task.done():
                        task.cancel()
                        pending_tasks.append(task)

            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

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

                self._loop = asyncio.get_running_loop()

                start_time_root_ns = time.time_ns()
                await self.workflow.context.database.update_workflow(
                    self.workflow.persistent_id, {"start_time": start_time_root_ns}
                )

                self.prov_workflow = YProvWorkflow(self.workflow.name, f'workflow_{self.workflow.name}')
                self.prov_workflow._start_time = sf_utils.get_date_from_ns(start_time_root_ns)
                self.prov_workflow._engineWMS = 'StreamFlow'
                self.prov_workflow._level = '0'
                if "config" in self.map_file: 
                    self.prov_workflow._resource_cwl_uri = self.map_file["config"]

                t_cwl = time.perf_counter()
                self.computed_cwl_deps = self._parse_cwl_for_dependencies()
                _yprov_log(f"YPROV [PARSER]: CWL graph parsing completed in {time.perf_counter() - t_cwl:.4f}s")

                for task_name, step in self.workflow.steps.items():
                    clean_name = step.name.lstrip('/')

                    if self._is_valid_cwl_step(clean_name):
                        self._attach_step_monitors(step)

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
                
                json_file_path = await self._flush_prov_json_async()

                if json_file_path and os.path.exists(json_file_path):
                    path = os.path.join(self.outdir, self.workflow.name + ".zip")

                    t_zip = time.perf_counter()
                    with ZipFile(path, "w") as archive:
                        archive.write(json_file_path, arcname="provenance.json")  
                        for src, dst in self.map_file.items():
                            if os.path.exists(src) and dst not in archive.namelist():
                                archive.write(src, dst)
                    
                    zip_dur = time.perf_counter() - t_zip
                    _yprov_log(f"Successfully generated final zip package in {zip_dur:.4f}s at: {path}")

                if self._flush_task is not None and not self._flush_task.done():
                    await self._flush_task

                try:
                    import concurrent.futures.process
                    atexit.unregister(concurrent.futures.process._python_exit)
                except Exception:
                    pass

                _yprov_log("Execution finished successfully. Returning output tokens.")
                _yprov_log(f"[PROV_FLUSHES] total_flushes={self._total_flush_count}")
                
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

                logger.error("\n" + "=" * 62)
                logger.error(" WORKFLOW EXECUTION FAILED")
                logger.error(f" Reason: {reason}")
                logger.error(" Provenance: yprov4wfs.json updated and saved successfully.")
                logger.error("=" * 62 + "\n")
                
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
                await asyncio.gather(
                    *(
                        asyncio.create_task(step.terminate(Status.CANCELLED))
                        for step in self.workflow.steps.values()
                        if not step.terminated
                    )
                )
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
                await asyncio.gather(
                    *(
                        asyncio.create_task(step.terminate(Status.CANCELLED))
                        for step in self.workflow.steps.values()
                        if not step.terminated
                    )
                )
                self._closed = True

        async def closed(self) -> bool:
            if self._closing is not None:
                await self._closing.wait()
            return self._closed