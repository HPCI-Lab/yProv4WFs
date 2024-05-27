import os.path
import uuid
import posixpath
import asyncio
import cwltool.context
import cwltool.process
import streamflow.core.utils
import streamflow.cwl.utils
from abc import abstractmethod
from zipfile import ZipFile
from typing import Any, MutableMapping, MutableSequence, TYPE_CHECKING, Optional
from streamflow.core.provenance import ProvenanceManager
from streamflow.core.workflow import Port, Status, Workflow as StreamFlowWorkflow
from streamflow.core.context import StreamFlowContext
from streamflow.core.exception import (WorkflowDefinitionException, WorkflowProvenanceException)
from streamflow.core.persistence import DatabaseLoadingContext, StreamFlowContext, DatabaseLoadingContext
from streamflow.log_handler import logger
from datamodel.workflow import Workflow
from datamodel.task import Task
from datamodel.data import Data, FileType
from datamodel.agent import Agent


def _get_cwl_entity_id(entity_id: str) -> str:
    tokens = entity_id.split("#")
    if len(tokens) > 1:
        return "#".join([tokens[0].split("/")[-1], tokens[1]])
    else:
        return tokens[0].split("/")[-1]
    
def _get_status(status: Status) -> str:
    if status == Status.COMPLETED:
        return "Completed"
    elif status == Status.FAILED:
        return "Failed"
    elif status in [Status.CANCELLED, Status.SKIPPED]:
        return "Cancelled or Skipped"
    else:
        raise WorkflowProvenanceException(f"Action status {status.name} not supported.")

class PROW4WFSProvenanceManagerStreamFlow(ProvenanceManager):
    def __init__(
        self,
        context: StreamFlowContext,
        db_context: DatabaseLoadingContext,
        workflows: MutableSequence[Workflow],
    ):
        super().__init__(context, db_context, workflows)
        self.map_file: MutableMapping[str, str] = {}

    @abstractmethod
    async def get_main_entity(self) -> MutableMapping[str, Any]: ...
    
    @abstractmethod
    async def add_initial_inputs(self, wf_id: int, workflow: Workflow) -> None: ...

        
    async def create_archive(
        self,
        outdir: str,
        filename: Optional[str],
        config: Optional[str],
        additional_files: Optional[MutableSequence[MutableMapping[str, str]]],
        additional_properties: Optional[MutableSequence[MutableMapping[str, str]]],
    ):
        if config:
            self.map_file[config] = config
  
        for wf in self.workflows:
            wf_obj = await self.context.database.get_workflow(wf.persistent_id)
            workflow = Workflow(wf_obj["name"], f'workflow_{wf_obj["name"]}')
            workflow._start_time = streamflow.core.utils.get_date_from_ns(wf_obj["start_time"])
            workflow._end_time = streamflow.core.utils.get_date_from_ns(wf_obj["end_time"])
            workflow._status = _get_status(Status(wf_obj["status"]))
            
            execution_wf = {
                "id": workflow._id,
                "status": workflow._status,
                "endTime": workflow._end_time,
                "name": workflow._name,
                "startTime": workflow._start_time,
            }
            print(execution_wf)

            for step in wf.steps:
                if s := wf.steps.get(step): 
                    for execution_wf in await self.context.database.get_executions_by_step(s.persistent_id):
                        task = Task(str(uuid.uuid4()), step)
                        task._start_time = streamflow.core.utils.get_date_from_ns(execution_wf["start_time"])
                        task._end_time = streamflow.core.utils.get_date_from_ns(execution_wf["end_time"])
                        task._status = _get_status(Status(execution_wf["status"]))
                        # task.set_enactor(workflow.get_enactor_by_id(step.enactor_id))
                        
                        execution_task = {
                            "id": task._id,
                            "status": task._status,
                            "endTime": task._end_time,
                            "name": task._name,
                            "startTime": task._start_time,
                        }
                        print(execution_task)
                                
                        # process Input and Output
                            
                        workflow.add_task(task) 
                
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, filename or (self.workflows[0].name + ".zip"))
        with ZipFile(path, "w") as archive:
            json_file_path = workflow.prov_to_json()  
            archive.write(json_file_path)  
            for src, dst in self.map_file.items():
                if os.path.exists(src):
                    if dst not in archive.namelist():
                        archive.write(src, dst)
                else:
                    logger.warning(f"File {src} does not exist.")
        print(f"Successfully created PROV4WFS archive at {path}")
        