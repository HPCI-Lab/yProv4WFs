from datamodel.node import Node
from datamodel.data import Data
from datamodel.task import Task
import prov.model as prov

#------------------WORKFLOW------------------–# 
class Workflow(Node):
    def __init__(self, id: str, name: str):
        super().__init__(id, name)
        self._inputs = []
        self._outputs = []
        self._tasks = []

    def add_input(self, data: Data):
        data.set_consumer(self)
        if data.is_input():
            self._inputs.append(data)

    def add_output(self, data: Data):
        data.set_producer(self)
        if data.is_output():
            self._outputs.append(data)

    def add_task(self, task: 'Task'):
        if self._tasks:
            last_task = self._tasks[-1]
            last_task.set_target(task)
            task.set_source(last_task)
        self._tasks.append(task)

    def to_prov(self):
        doc = prov.ProvDocument()
        doc.set_default_namespace('http://anotherexample.org/')
        #to add namespaces
        #doc.add_namespace("dcterms", "http://purl.org/dc/terms/")

        # Add workflow as activity
        # doc.activity(identifier, startTime=None, endTime=None, other_attributes=None)
        doc.activity(self._id, self._start_time, self._end_time, {'prov:label': self._name, 'prov:type': 'prov:Activity'})

        # Add tasks as activities and enactors as agents
        for task in self._tasks:
            doc.activity(task._id, task._start_time, task._end_time, {'prov:label': task._name, 'prov:type': 'prov:Activity'})
            if task._enactor is not None:
                doc.agent(task._enactor._id, {'prov:label': task._enactor._name, 'prov:type': 'prov:Agent'})

                # Add wasAssociatedWith relation between task and enactor
                doc.wasAssociatedWith(task._id, task._enactor._id)

                # Add wasAttributedTo relations between enactor and data items
                for data_item in task._enactor._attributed_to:
                    doc.entity(data_item._id, {'prov:label': data_item._name, 'prov:type': 'prov:Entity'})
                    doc.wasAttributedTo(data_item._id, task._enactor._id)

                # Add actedOnBehalfOf relations between enactor and the enactors it acted for
                if task._enactor._acted_for is not None:
                    doc.agent(task._enactor._acted_for._id, {'prov:label': task._enactor._acted_for._name, 'prov:type': 'prov:Agent'})
                    doc.actedOnBehalfOf(task._enactor._id, task._enactor._acted_for._id)

            # Add wasInformedBy relation between tasks
            if task._source is not None:
                doc.wasInformedBy(task._id, task._source._id)

            # Add used and wasGeneratedBy relations for inputs and outputs
            for data_item in task._inputs:
                doc.entity(data_item._id, {'prov:label': data_item._name, 'prov:type': 'prov:Entity'})
                doc.used(task._id, data_item._id)
            for data_item in task._outputs:
                doc.entity(data_item._id, {'prov:label': data_item._name, 'prov:type': 'prov:Entity'})
                doc.wasGeneratedBy(data_item._id, task._id)

        return doc.serialize(format='json')
    