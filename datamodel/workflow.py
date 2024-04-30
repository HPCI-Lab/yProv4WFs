from datamodel.node import Node
from datamodel.data import Data
from datamodel.task import Task

#------------------WORKFLOW------------------–# 
class Workflow(Node):
    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self._inputs = []
        self._outputs = []
        self._tasks = []

    def get_input(self, data: 'Data'):
        if data.is_input(self):
            self._inputs.append(data)

    def get_output(self, data: 'Data'):
        if data.is_output(self):
            self._outputs.append(data)

    def add_task(self, task: 'Task'):
        if self._tasks:
            last_task = self._tasks[-1]
            last_task.set_target(task)
            task.set_source(last_task)
        self._tasks.append(task)

    def next_task(self, current_task: 'Task'):
        current_index = self._tasks.index(current_task)
        if current_index + 1 < len(self._tasks):
            return self._tasks[current_index + 1]
        return None

    def previous_task(self, current_task: 'Task'):
        current_index = self._tasks.index(current_task)
        if current_index - 1 >= 0:
            return self._tasks[current_index - 1]
        return None
