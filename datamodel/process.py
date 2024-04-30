from datamodel.workflow import Workflow
from datamodel.node import Node

#------------------PROCESS------------------–#

class Process(Node):
    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self._workflows = []

    def get_workflows(self):
        return self._workflows
    
    def add_workflow(self, workflow: 'Workflow'):
        if self._workflows:
            last_workflow = self._workflows[-1]
            last_workflow.set_target(workflow)
            workflow.set_source(last_workflow)
        self._tasks.append(workflow)

    def next_workflow(self, current_workflow: 'Workflow'):
        current_index = self._workflows.index(current_workflow)
        if current_index + 1 < len(self._workflows):
            return self._workflows[current_index + 1]
        return None

    def previous_workflow(self, current_task: 'Workflow'):
        current_index = self._workflows.index(current_task)
        if current_index - 1 >= 0:
            return self._workflows[current_index - 1]
        return None

