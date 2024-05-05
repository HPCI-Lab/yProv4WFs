from typing import Union
from datamodel.task import Task
from datamodel.workflow import Workflow
from datamodel.process import Process
from datamodel.data import Data

#------------------ENACTOR------------------–#
class Enactor:
    def __init__(self, id: str, name: str):
        self._id = id
        self._name = name
        self._acted_for = None
        self._attributed_to = []
        # mode 1 - general
        self._associated_with = []
        
        # mode 2 - specific
        # self._tasks = []
        # self._workflows = []
        # self._processes = []

    def set_acted_for(self, enactor: 'Enactor'):
        self._acted_for = enactor
    
    def attribute_to(self, data: 'Data'):
        self._attributed_to.append(data)
        data.set_enactor(self)

    # mode 1:
    # Associate the agent with a task, workflow or process, but we cannot know the type of the node
    def associate_with(self, node: Union['Task', 'Workflow', 'Process']):
        node.set_enactor(self)
        self._associated_with.append(node)

    # mode 2:
    # we can know if the node is a task, workflow or process, different methods for each type

    # def initiate_task(self, task: 'Task'):
    #     task.set_enactor(self)
    #     self._tasks.append(task)

    # def initiate_workflow(self, workflow: 'Workflow'):
    #     workflow.set_enactor(self)
    #     self._workflows.append(workflow)
    
    # def initiate_process(self, process: 'Process'):
    #     process.set_enactor(self)
    #     self._processes.append(process)