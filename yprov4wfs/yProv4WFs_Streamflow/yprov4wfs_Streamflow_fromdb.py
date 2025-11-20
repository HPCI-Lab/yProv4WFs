import os.path
import uuid
import streamflow.core.utils
import streamflow.cwl.utils
from abc import abstractmethod
from zipfile import ZipFile
from typing import Any, MutableMapping, MutableSequence, TYPE_CHECKING, Optional
from streamflow.core.provenance import ProvenanceManager
from streamflow.core.workflow import Status, Workflow as StreamFlowWorkflow
from streamflow.core.context import StreamFlowContext
from streamflow.core.exception import WorkflowProvenanceException
from streamflow.core.persistence import DatabaseLoadingContext, StreamFlowContext, DatabaseLoadingContext
from streamflow.log_handler import logger

#yprov4wfs imports
from yprov4wfs.datamodel.workflow import Workflow
from yprov4wfs.datamodel.task import Task
from yprov4wfs.datamodel.data import Data, FileType
from yprov4wfs.datamodel.core import Agent


class yProv4WFsProvenanceManager(ProvenanceManager):
    def __init__(
        self,
        context: StreamFlowContext,
        db_context: DatabaseLoadingContext,
        workflows: MutableSequence[Workflow],
    ):
        super().__init__(context, db_context, workflows)
        self.map_file: MutableMapping[str, str] = {}
        self.prov_workflow = None
        
    @abstractmethod
    async def get_main_entity(self) -> MutableMapping[str, Any]: ...
    
    @abstractmethod
    async def add_initial_inputs(self, wf_id: int, workflow: Workflow) -> None: ...

    def _get_action_status(self, status: Status) -> str:
        if status == Status.COMPLETED:
            return "Completed"
        elif status == Status.FAILED:
            return "Failed"
        elif status in [Status.CANCELLED, Status.SKIPPED]:
            return "Cancelled or Skipped"
        else:
            raise WorkflowProvenanceException(f"Action status {status.name} not supported.")
    

    async def populate_prov_workflow(self):
        
        for wf in self.workflows:
            wf_obj = await self.context.database.get_workflow(wf.persistent_id)
            self.prov_workflow = Workflow(wf_obj["name"], f'workflow_{wf_obj["name"]}')
            self.prov_workflow._start_time = streamflow.core.utils.get_date_from_ns(wf_obj["start_time"])
            self.prov_workflow._end_time = streamflow.core.utils.get_date_from_ns(wf_obj["end_time"])
            self.prov_workflow._status = self._get_action_status(Status(wf_obj["status"]))
            self.prov_workflow._engineWMS = 'StreamFlow'
            self.prov_workflow._level = '0'
            self.prov_workflow._resource_cwl_uri = self.map_file["config"]           
            
            # maps for dependency building for finding the connection between the tasks
            stepid_to_task: dict[int, Task] = {}
            
            for input in await self.context.database.get_input_ports(wf.persistent_id):
                data_in = Data(str(uuid.uuid4()), input["name"])
                self.prov_workflow.add_input(data_in)
                data_in.add_consumer(self.prov_workflow._id)
                execution_input = {
                    "id": data_in._id,
                    "name": data_in._name,
                    "consumer": data_in._consumers
                }
                print(execution_input)
                            
            for output in await self.context.database.get_output_ports(wf.persistent_id):
                data_out = Data(str(uuid.uuid4()), output["name"])
                self.prov_workflow.add_output(data_out)
                data_out.set_producer(self.prov_workflow._id)
                execution_output = {
                    "id": data_out._id,
                    "name": data_out._name,
                    "producer": data_out._producer
                }
                #print(execution_output)
            
            execution_wf = {
                "id": self.prov_workflow._id,
                "status": self.prov_workflow._status,
                "endTime": self.prov_workflow._end_time,
                "name": self.prov_workflow._name,
                "startTime": self.prov_workflow._start_time,
                "engineWMS": self.prov_workflow._engineWMS,
                "resource_uri": self.prov_workflow._resource_cwl_uri,
                "input": {i: i for i in self.prov_workflow._inputs},
                "output": {o: o for o in self.prov_workflow._outputs},
                "level": self.prov_workflow._level,
            }
            #print(execution_wf)
            
            for task_name in wf.steps:
                if s := wf.steps.get(task_name):
                    for execution_wf in await self.context.database.get_executions_by_step(s.persistent_id):
                        task = Task(str(uuid.uuid4()), task_name)
                        task._start_time = streamflow.core.utils.get_date_from_ns(execution_wf["start_time"])
                        task._end_time = streamflow.core.utils.get_date_from_ns(execution_wf["end_time"])
                        task._status = self._get_action_status(Status(execution_wf["status"]))
                        task._level = '1'
                        # record mapping from StreamFlow step to Task
                        # Inside the DB we have the ids for the steps but yprov4wfs works with Task objects
                        # so we use this Dictionary to map the step ids to the Task objects.
                        # example: {1: <Task bejct for step 1>, 2: <Task bejct for step 1>}
                        stepid_to_task[s.persistent_id] = task
                    
                        
                        # Inputs - they use to create dependencies
                        input_rows = await self.context.database.get_input_ports(s.persistent_id)
                        for input_row in input_rows:
                            data_in = Data(str(uuid.uuid4()), input_row["name"])
                            task.add_input(data_in)
                            data_in.add_consumer(task)

                        output_rows = await self.context.database.get_output_ports(s.persistent_id)   
                        for output_row in output_rows:
                            data_out = Data(str(uuid.uuid4()), output_row["name"])
                            task.add_output(data_out)
                            data_out.set_producer(task)
                            
                        self.prov_workflow.add_task(task)
            
            #
            db = self.context.database
            # Avoid duplicates 
            created_edges: set[tuple[str, str]] = set()

            # SECOND LOOP: build dependencies based on the tokens
            for task_name in wf.steps:
                if not (s := wf.steps.get(task_name)):
                    continue

                producer_task = stepid_to_task.get(s.persistent_id)
                if producer_task is None:
                    continue

                output_ports = await db.get_output_ports(s.persistent_id)
                for out_port in output_ports:
                    port_id = out_port["port"]


                    token_ids = await db.get_port_tokens(port_id)
                    for token_id in token_ids:

                        dependers = await db.get_dependers(token_id)

                        for dep in dependers:
                            dep_dict = dict(dep)
                            depender_token_id = dep_dict.get("depender") or dep_dict.get("id")
                            if depender_token_id is None:
                                continue

                            depender_port = await db.get_port_from_token(depender_token_id)
                            if depender_port is None:
                                print(f"No port found for token {depender_token_id}")

                            depender_port_id = dict(depender_port)["id"]

                            input_steps = await db.get_input_steps(depender_port_id)
                            for step_row in input_steps:
                                consumer_step_id = step_row["step"]

                                consumer_task = stepid_to_task.get(consumer_step_id)
                                if consumer_task is None:
                                    continue 

                                if consumer_task is producer_task:
                                    continue 

                                edge_key = (producer_task._id, consumer_task._id)
                                if edge_key in created_edges:
                                    continue
                                created_edges.add(edge_key)

                                consumer_task.add_prev(producer_task)
                                producer_task.add_next(consumer_task)

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
            archive.write(json_file_path)  
            for src, dst in self.map_file.items():
                if os.path.exists(src):
                    if dst not in archive.namelist():
                        archive.write(src, dst)
                else:
                    logger.warning(f"File {src} does not exist.")
        print(f"Successfully created yProv4WFs archive at {path}")
        