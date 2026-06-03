"""
This version includes the fix for generating a 
connected graph even in the presence of nested sub-workflows.
"""

import os
import os.path
import uuid
import asyncio
import yaml
from abc import abstractmethod
from zipfile import ZipFile
from typing import Any, MutableMapping, MutableSequence, Optional

import streamflow.core.utils
import streamflow.cwl.utils
from streamflow.core.provenance import ProvenanceManager
from streamflow.core.workflow import Status, Workflow as StreamFlowWorkflow
from streamflow.core.context import StreamFlowContext
from streamflow.core.exception import WorkflowProvenanceException
from streamflow.core.persistence import DatabaseLoadingContext
from streamflow.log_handler import logger

from yprov4wfs.datamodel.workflow import Workflow
from yprov4wfs.datamodel.task import Task
from yprov4wfs.datamodel.data import Data, FileType
from yprov4wfs.datamodel.agent import Agent 

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
        logger.info("Starting new yprov4wfs version")
        
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

        # Get all cwl files in the current folder, if present
        cwl_files = [f for f in os.listdir('.') if f.endswith('.cwl')]
        if not cwl_files:
            logger.warning("YPROV: No CWL file found in the current directory.")
            return dependencies
        
        # map out all short names to their absolute execution paths
        # this is to make a clear mapping between the "absolute" path stored in 
        # StreamFlow database and "relative" path of the cwl files
        full_path_map = {}
        for full_path in self.tasks_by_step_name.keys():
            # This mapping assumes unique short names per layer. 
            # This is safe by design: the CWL specification strictly enforces unique step identifiers.
            short_name = full_path.lstrip('/').split('/')[-1]
            full_path_map[short_name] = full_path
        logger.info(f"Full path map: {full_path_map}")

        for filename in cwl_files:
            try:
                with open(filename, 'r') as f:
                    data = yaml.safe_load(f)
                if data.get('class') != 'Workflow': 
                    continue

                steps = data.get('steps', {})
                steps_items = steps.items() if isinstance(steps, dict) else [(s['id'], s) for s in steps]

                # evaluate each step found inside the target
                for step_id, step_val in steps_items:
                    # step_id is the step name
                    # step_val contains other information e.g cwl file, inputs and outputs
                    short_step_name = step_id.split('/')[-1]
                    
                    # get absolute path of the step from the map
                    full_step_name = full_path_map.get(short_step_name)

                    if not full_step_name:
                        logger.debug(f"YPROV: Step {short_step_name} not found in execution map, skipping.")
                        continue
                    
                    if full_step_name not in dependencies:
                        dependencies[full_step_name] = []
                    
                    # derive the prefix specifically for this step's neighborhood
                    # example: "/nested_b/nested_d/step_d1" -> "/nested_b/nested_d"
                    current_prefix = full_step_name.rsplit('/', 1)[0]
                    
                    inputs = step_val.get('in', [])
                    input_list = inputs if isinstance(inputs, list) else [{'source': v} for v in inputs.values()]

                    for inp in input_list:
                        src = inp.get('source') if isinstance(inp, dict) else inp
                        if src:
                            sources = src if isinstance(src, list) else [src]
                            for s in sources:
                                # CASE 1: Standard dependency within the same file
                                # this means that we can already extract the dependency as it is "already written"
                                if '/' in s:
                                    parent_short_name = s.split('/')[0]
                                    full_parent_name = f"{current_prefix}/{parent_short_name}" if current_prefix else f"/{parent_short_name}"
                                    if full_parent_name in full_path_map.values() and full_parent_name not in dependencies[full_step_name]:
                                        dependencies[full_step_name].append(full_parent_name)
                                
                                # CASE 2: Nested input boundary fallback (e.g. "in_data" with no slash)
                                # the dependency is NOT coming from explicitly "inside the file" but they
                                # are fed directly from the sub-workflows's outer boundary (or a global workflow input)
                                elif current_prefix and current_prefix != '/':
                                    # verify step depth to ensure fallback only executes on deeply nested workflows (>=2 slashes)
                                    # prevents parallel root steps from misinterpreting top-level inputs as cross-talk
                                    # example: "/nested_b" has 1 slash (Depth 2 task) -> skip fallback
                                    # example: "/nested_b/nested_d" has 2 slashes (Depth 3 task) -> process fallback
                                    if current_prefix.count('/') >= 2:
                                        parent_environment = current_prefix.rsplit('/', 1)[0]
                                        
                                        for short_name, full_path in full_path_map.items():
                                            # Enforce that the parent must live exactly in the parent environment
                                            # and not "inside" itself and/or itself
                                            if (full_path.startswith(parent_environment) and 
                                                not full_path.startswith(current_prefix) and 
                                                full_path != full_step_name):
                                                
                                                if full_path not in dependencies[full_step_name]:
                                                    logger.info(f"Boundary dependency resolved: {full_path} -> {full_step_name}")
                                                    dependencies[full_step_name].append(full_path)
            except Exception as e:
                logger.warning(f"YPROV: Error parsing file {filename}: {e}")
                continue

        logger.info(f"Dependencies list: {dependencies}")
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
            
            # ------------------------------------------------------------------------------
            # extract both input and output ports of the given workflow id
            # by using a combination of get functions
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
                    data_in.set_consumer(self.prov_workflow._id)
                
                for output in outputs:
                    data_out = Data(str(uuid.uuid4()), output["name"])
                    self.prov_workflow.add_output(data_out)
                    data_out.set_producer(self.prov_workflow._id)
            # ------------------------------------------------------------------------------


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
                            data_in.set_consumer(task._id)

                        outputs = await self.context.database.get_output_ports(s.persistent_id)
                        for output_port in outputs:
                            data_out = Data(str(uuid.uuid4()), output_port["name"])
                            task.add_output(data_out)
                            data_out.set_producer(task._id)

            cwl_deps = self._parse_cwl_for_dependencies()

            for child_name, parents in cwl_deps.items():
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
        
        with ZipFile(path, "w") as archive:
            json_file_path = self.prov_workflow.prov_to_json()  
            archive.write(json_file_path, arcname="provenance.json")  
            for src, dst in self.map_file.items():
                if os.path.exists(src):
                    if dst not in archive.namelist():
                        archive.write(src, dst)
                else:
                    logger.warning(f"File {src} does not exist.")
        
        print(f"Successfully created yProv4WFs archive at {path}")