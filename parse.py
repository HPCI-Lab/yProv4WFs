import json
from datetime import datetime
from datamodel.node import Node
from datamodel.workflow import Workflow
from datamodel.task import Task
from datamodel.data import Data, FileType
from datamodel.enactor import Enactor

class WorkflowParser:
    def __init__(self, config):
        self.config = config

    def parse(self, document):
        # Load the JSON-LD document
        with open(document, 'r') as f:
            data = json.load(f)

        # Create a instance of the Workflow class
        workflow = Workflow(id=data[self.config['workflow_id']], name=data[self.config['workflow_name']], start_time=data[self.config['workflow_start_time']], end_time=data[self.config['workflow_end_time']])

        tasks = {}
        data_items = {}

        # Iterate over the steps in the workflow
        for step in data[self.config['graph']]:
            if step[self.config['type']] in self.config['action']:
                # Create a new instance of the Task class
                task = Task(id=step[self.config['task_id']], name=step[self.config['name']], start_time=datetime.now(), end_time=datetime.now(), actor=step[self.config['actor']])
                
                # Add input and output data to the task
                for input in step[self.config['object']]:
                    input_data = Data(id=input[self.config['input_id']], name=input[self.config['name']], type=FileType(extension='txt', mime_type='text/plain'), producer=None, consumer=task)
                    task.get_input(input_data)
                    data_items[input_data.id] = input_data
                
                for output in step[self.config['result']]:
                    output_data = Data(id=output[self.config['output_id']], name=output[self.config['name']], type=FileType(extension='txt', mime_type='text/plain'), producer=task, consumer=None)
                    task.get_output(output_data)
                    data_items[output_data.id] = output_data
                
                # Add Task to Workflow
                workflow.add_task(task)
                tasks[task.id] = task

        # Set task dependencies
        for task in tasks.values():
            for data_item in task.inputs + task.outputs:
                if data_item.is_input() and data_item.consumer is not None:
                    task.dependencies.append(data_item.consumer)
                elif data_item.is_output() and data_item.producer is not None:
                    task.dependencies.append(data_item.producer)

        return workflow

# Configuration specific for Streamflow RO-Crate json output
#TODO: understand if it can work or not effectively
config = {
    'graph': '@graph',
    'type': '@type',
    'action': ['CreateAction', 'ControlAction'],
    'task_id': '@id',
    'name': 'name',
    'object': 'object',
    'input': 'input',
    'input_id': '@id',
    'result': 'result',
    'output': 'output',
    'output_id': '@id',
    'actor': 'instrument',
    'workflow_id': '@id',
    'workflow_name': 'name',
    'workflow_start_time': 'startTime',
    'workflow_end_time': 'endTime'
}
parser = WorkflowParser(config)
workflow = parser.parse('workflow.jsonld')