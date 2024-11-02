import asyncio
import time
import uuid
from datetime import datetime

from typing import MutableSequence, TYPE_CHECKING
from typing import Any, MutableMapping

import streamflow.core.utils
from streamflow.core import utils
from streamflow.core.workflow import Executor, Status
from streamflow.core.exception import WorkflowExecutionException
from streamflow.workflow.executor import StreamFlowExecutor
from streamflow.workflow.step import ExecuteStep

from yprov4wfs.datamodel.workflow import Workflow
from yprov4wfs.datamodel.task import Task
from yprov4wfs.datamodel.data import Data, FileType
from yprov4wfs.datamodel.agent import Agent


class yProv4WfsExecutor(StreamFlowExecutor):
    def __init__(self, workflow: Executor):
        super().__init__(workflow)
        self.prov_workflow = None
        self.runtime_data = {}
        self.execution_times = {}
    
    def get_status(self, status: Status) -> str:
        if status == Status.COMPLETED:
            return "Completed"
        elif status == Status.FAILED:
            return "Failed"
        elif status in [Status.CANCELLED, Status.SKIPPED]:
            return "Cancelled or Skipped"
        else:
            return "Error"
    
    async def populate_prov_workflow(self):
        self.prov_workflow = Workflow(self.workflow.name, f'workflow_{self.workflow.name}')
        self.prov_workflow._start_time = streamflow.core.utils.get_date_from_ns(self.runtime_data['start_time'])
        
        # for task in self.workflow.steps.values():
        for task in self.workflow.steps:
            if t := self.workflow.steps.get(task):
                prov_task = Task(str((uuid.uuid4())), t.name)
                prov_task._start_time = streamflow.core.utils.get_date_from_ns(self.execution_times[t.name]['start'])
                prov_task._end_time = streamflow.core.utils.get_date_from_ns(self.execution_times[t.name]['end'])
                prov_task._status = self.get_status(t.status)
                
                for inp_key, inp_port in t.input_ports.items():
                    if i := t.get_input_port(inp_key):
                        # prov_input = Data(str((uuid.uuid4())),i.name)
                        prov_input = Data(i.name,i.name)
                        prov_task.add_input(prov_input)
                        prov_input.set_consumer(prov_task._id)
                        
                        ex_d_in = {
                            "id": prov_input._id,
                            "name": prov_input._name,
                            "consumer": prov_input._consumer
                        }
                        print(ex_d_in)
                for out_key, out_value in t.output_ports.items():
                    if o := t.get_output_port(out_key):
                        prov_output = Data(o.name,o.name)
                        prov_task.add_output(prov_output)
                        prov_output.set_producer(prov_task._id)
                        ex_d_out = {
                            "id": prov_output._id,
                            "name": prov_output._name,
                            "producer": prov_output._producer
                        }
                        print(ex_d_out)
       
            self.prov_workflow.add_task(prov_task)
            
            
            execution_task = {
                "id": prov_task._id,
                "status": prov_task._status,
                "endTime": prov_task._end_time,
                "name": prov_task._name,
                "startTime": prov_task._start_time,
                "inputs": [input for input in prov_task._inputs],
                "outputs": [output for output in prov_task._outputs]
            }
            print(execution_task)
        
        self.prov_workflow._end_time = streamflow.core.utils.get_date_from_ns(self.runtime_data['end_time'])
        self.prov_workflow._status = self.get_status(self.runtime_data['status']) 
        
        execution_wf = {
                "id": self.prov_workflow._id,
                "status": self.prov_workflow._status,
                "endTime": self.prov_workflow._end_time,
                "name": self.prov_workflow._name,
                "startTime": self.prov_workflow._start_time,
            }
        print(execution_wf)
            
            
        return self.prov_workflow



    async def streamflowExecutor_run(self) -> MutableMapping[str, Any]:
        try:
            output_tokens = {}
            # Execute workflow
            start_time = time.time_ns()
            await self.workflow.context.database.update_workflow(
                self.workflow.persistent_id, {"start_time": start_time}
            )
            self.runtime_data['start_time'] = start_time
            for step in self.workflow.steps.values():
                
                ex_start_time = time.time_ns()
                
                execution = asyncio.create_task(
                    self._handle_exception(asyncio.create_task(step.run())),
                    name=step.name,
                )
                
                ex_end_time = time.time_ns()
                self.execution_times[step.name] = {'start': ex_start_time, 'end': ex_end_time}
                
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
                end_time = time.time_ns()
                await self.workflow.context.database.update_workflow(
                    self.workflow.persistent_id,
                    {"status": Status.COMPLETED.value, "end_time": end_time},
                )
                
                self.runtime_data['end_time'] = end_time
                self.runtime_data['status'] = Status.COMPLETED
            # Print output tokens
            return output_tokens
        except Exception:
            if self.workflow.persistent_id:
                end_time = time.time_ns()
                await self.workflow.context.database.update_workflow(
                    self.workflow.persistent_id,
                    {"status": Status.FAILED.value, "end_time": end_time},
                )
                self.runtime_data['end_time'] = end_time
                self.runtime_data['status'] = Status.FAILED
            if not self.closed:
                await self._shutdown()
            raise

    

    async def run(self) -> MutableMapping[str, Any]:
        output_tokens = await self.streamflowExecutor_run()
        self.prov_workflow = await self.populate_prov_workflow()
        # Generate provenance JSON file
        self.prov_workflow.prov_to_json()
        return output_tokens
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    # class Prov4WfsExecutor(StreamFlowExecutor):
    # def __init__(self, workflow: Executor):
    #     super().__init__(workflow)
    #     self.prov_workflow = None
    #     self.runtime_data = {}
    #     self.execution_times = {}
    
    # def get_status(self, status: Status) -> str:
    #     if status == Status.COMPLETED:
    #         return "Completed"
    #     elif status == Status.FAILED:
    #         return "Failed"
    #     elif status in [Status.CANCELLED, Status.SKIPPED]:
    #         return "Cancelled or Skipped"
    #     else:
    #         return "Error"
    
    # async def populate_prov_workflow(self):
    #     self.prov_workflow = Workflow(self.workflow.name, f'workflow_{self.workflow.name}')
    #     self.prov_workflow._start_time = streamflow.core.utils.get_date_from_ns(self.runtime_data['start_time'])
        
    #     # for task in self.workflow.steps.values():
    #     for task in self.workflow.steps:
    #         if t := self.workflow.steps.get(task):
    #             prov_task = Task(str((uuid.uuid4())), t.name)
    #             prov_task._start_time = streamflow.core.utils.get_date_from_ns(self.execution_times[t.name]['start'])
    #             prov_task._end_time = streamflow.core.utils.get_date_from_ns(self.execution_times[t.name]['end'])
    #             prov_task._status = self.get_status(t.status)
                
    #             for inp_key, inp_port in t.input_ports.items():
    #                 if i := t.get_input_port(inp_key):
    #                     prov_input = Data(str((uuid.uuid4())),i.name)
    #                     # prov_input = Data(i.name,i.name)
    #                     prov_task.add_input(prov_input)
    #                     prov_input.set_consumer(prov_task._id)
                        
    #                     ex_d_in = {
    #                         "id": prov_input._id,
    #                         "name": prov_input._name,
    #                         "consumer": prov_input._consumer
    #                     }
    #                     print(ex_d_in)
    #             for out_key, out_value in t.output_ports.items():
    #                 if o := t.get_output_port(out_key):
    #                     prov_output = Data(str((uuid.uuid4())),o.name)
    #                     prov_task.add_output(prov_output)
    #                     prov_output.set_producer(prov_task._id)
    #                     ex_d_out = {
    #                         "id": prov_output._id,
    #                         "name": prov_output._name,
    #                         "producer": prov_output._producer
    #                     }
    #                     print(ex_d_out)
       
    #         self.prov_workflow.add_task(prov_task)
            
            
    #         execution_task = {
    #             "id": prov_task._id,
    #             "status": prov_task._status,
    #             "endTime": prov_task._end_time,
    #             "name": prov_task._name,
    #             "startTime": prov_task._start_time,
    #             "inputs": [input for input in prov_task._inputs],
    #             "outputs": [output for output in prov_task._outputs]
    #         }
    #         print(execution_task)
        
    #     self.prov_workflow._end_time = streamflow.core.utils.get_date_from_ns(self.runtime_data['end_time'])
    #     self.prov_workflow._status = self.get_status(self.runtime_data['status']) 
        
    #     execution_wf = {
    #             "id": self.prov_workflow._id,
    #             "status": self.prov_workflow._status,
    #             "endTime": self.prov_workflow._end_time,
    #             "name": self.prov_workflow._name,
    #             "startTime": self.prov_workflow._start_time,
    #         }
    #     print(execution_wf)
            
            
    #     return self.prov_workflow



    # async def streamflowExecutor_run(self) -> MutableMapping[str, Any]:
    #     try:
    #         output_tokens = {}
    #         # Execute workflow
    #         start_time = time.time_ns()
    #         await self.workflow.context.database.update_workflow(
    #             self.workflow.persistent_id, {"start_time": start_time}
    #         )
    #         self.runtime_data['start_time'] = start_time
    #         for step in self.workflow.steps.values():
                
    #             ex_start_time = time.time_ns()
                
    #             execution = asyncio.create_task(
    #                 self._handle_exception(asyncio.create_task(step.run())),
    #                 name=step.name,
    #             )
                
    #             ex_end_time = time.time_ns()
    #             self.execution_times[step.name] = {'start': ex_start_time, 'end': ex_end_time}
                
    #             self.executions.append(execution)
    #         if self.workflow.persistent_id:
    #             await self.workflow.context.database.update_workflow(
    #                 self.workflow.persistent_id, {"status": Status.RUNNING.value}
    #             )
    #         # If workflow has output ports
    #         if self.workflow.output_ports:
    #             # Retrieve output tokens
    #             output_consumer = utils.random_name()
                
    #             for port_name, port in self.workflow.get_output_ports().items():
    #                 self.output_tasks[port_name] = asyncio.create_task(
    #                     self._handle_exception(
    #                         asyncio.create_task(port.get(output_consumer))
    #                     ),
    #                     name=port_name,
    #                 )
    #             while not self.closed:
    #                 output_tokens = await self._wait_outputs(
    #                     output_consumer, output_tokens
    #                 )
    #         # Otherwise simply wait for all tasks to finish
    #         else:
    #             await asyncio.gather(*self.executions)
    #         # Check if workflow terminated properly
    #         for step in self.workflow.steps.values():
    #             if step.status in [Status.FAILED, Status.CANCELLED]:
    #                 raise WorkflowExecutionException("FAILED Workflow execution")
    #         if self.workflow.persistent_id:
    #             end_time = time.time_ns()
    #             await self.workflow.context.database.update_workflow(
    #                 self.workflow.persistent_id,
    #                 {"status": Status.COMPLETED.value, "end_time": end_time},
    #             )
                
    #             self.runtime_data['end_time'] = end_time
    #             self.runtime_data['status'] = Status.COMPLETED
    #         # Print output tokens
    #         return output_tokens
    #     except Exception:
    #         if self.workflow.persistent_id:
    #             end_time = time.time_ns()
    #             await self.workflow.context.database.update_workflow(
    #                 self.workflow.persistent_id,
    #                 {"status": Status.FAILED.value, "end_time": end_time},
    #             )
    #             self.runtime_data['end_time'] = end_time
    #             self.runtime_data['status'] = Status.FAILED
    #         if not self.closed:
    #             await self._shutdown()
    #         raise

    

    # async def run(self) -> MutableMapping[str, Any]:
    #     output_tokens = await self.streamflowExecutor_run()
    #     self.prov_workflow = await self.populate_prov_workflow()
    #     # Generate provenance JSON file
    #     self.prov_workflow.prov_to_json()
    #     return output_tokens
    