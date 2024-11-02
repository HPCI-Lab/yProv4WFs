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
from yprov4wfs.datamodel.workflow import Workflow
from yprov4wfs.datamodel.task import Task
from yprov4wfs.datamodel.data import Data, FileType
from yprov4wfs.datamodel.agent import Agent


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
            
            for input in await self.context.database.get_input_ports(wf.persistent_id):
                data_in = Data(str(uuid.uuid4()), input["name"])
                self.prov_workflow.add_input(data_in)
                data_in.set_consumer(self.prov_workflow._id)
                execution_input = {
                    "id": data_in._id,
                    "name": data_in._name,
                    "consumer": data_in._consumer
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
                print(execution_output)
            
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
            print(execution_wf)
            
            for task in wf.steps:
                if s := wf.steps.get(task):
                    for execution_wf in await self.context.database.get_executions_by_step(s.persistent_id):
                        task = Task(str(uuid.uuid4()), task)
                        task._start_time = streamflow.core.utils.get_date_from_ns(execution_wf["start_time"])
                        task._end_time = streamflow.core.utils.get_date_from_ns(execution_wf["end_time"])
                        task._status = self._get_action_status(Status(execution_wf["status"]))
                        task._level = '1'
                        # task.set_enactor(workflow.get_enactor_by_id(step.enactor_id))
                        
                        execution_task = {
                            "id": task._id,
                            "status": task._status,
                            "endTime": task._end_time,
                            "name": task._name,
                            "startTime": task._start_time,
                            "level": task._level
                        }
                        print(execution_task)
                   
                        for input in await self.context.database.get_input_ports(s.persistent_id):
                            data_in = Data(str(uuid.uuid4()), input["name"])
                            task.add_input(data_in)
                            data_in.set_consumer(task._id)
                            execution_input = {
                                "id": data_in._id,
                                "name": data_in._name,
                                "consumer": data_in._consumer
                            }
                            print(execution_input)
                            
                        for output in await self.context.database.get_output_ports(s.persistent_id):
                            data_out = Data(str(uuid.uuid4()), output["name"])
                            task.add_output(data_out)
                            data_out.set_producer(task._id)
                            execution_output = {
                                "id": data_out._id,
                                "name": data_out._name,
                                "producer": data_out._producer
                            }
                            print(execution_output)
                            
                        self.prov_workflow.add_task(task)
                        
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
        