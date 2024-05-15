from datamodel.node import Node
from datamodel.data import Data
from datamodel.task import Task
import prov.model as prov
import json

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
        
        doc.activity(self._id, self._start_time, self._end_time, {
            'prov:label': self._name,
            'prov:type': 'prov:Activity'
        })
        # Add tasks as activities and enactors as agents
        for task in self._tasks:
            doc.activity(task._id, task._start_time, task._end_time, {
                'prov:label': task._name,
                'prov:type': 'prov:Activity'
                })
            if task._enactor is not None:
                doc.agent(task._enactor._id, {
                    'prov:label': task._enactor._name,
                    'prov:type': 'prov:Agent'
                })

                # Add wasAttributedTo relations between enactor and data items
                for data_item in task._enactor._attributed_to:
                    doc.entity(data_item._id, {
                        'prov:label': data_item._name,
                        'prov:type': 'prov:Entity'
                    })
                    doc.wasAttributedTo(data_item._id, task._enactor._id)

                # Add actedOnBehalfOf relations between enactor and the enactors it acted for
                if task._enactor._acted_for is not None:
                    doc.agent(task._enactor._acted_for._id, {
                        'prov:label': task._enactor._acted_for._name,
                        'prov:type': 'prov:Agent'
                    })
                    doc.actedOnBehalfOf(task._enactor._id, task._enactor._acted_for._id)

                # Add wasAssociatedWith relation between task and enactor
                doc.wasAssociatedWith(task._id, task._enactor._id)

            # Add wasInformedBy relation between tasks
            if task._source is not None:
                doc.wasInformedBy(task._id, task._source._id)

            # Add used and wasGeneratedBy relations for inputs and outputs
            for data_item in task._inputs:
                doc.entity(data_item._id, {
                        'prov:label': data_item._name,
                        'prov:type': 'prov:Entity'
                })
                doc.used(task._id, data_item._id)
            for data_item in task._outputs:
                doc.entity(data_item._id, {
                        'prov:label': data_item._name,
                        'prov:type': 'prov:Entity'
                })
            doc.wasGeneratedBy(data_item._id, task._id)

        return doc.serialize(format='json')
    

    def prov_to_json(self):
        prov_dict = json.loads(self.to_prov())
        with open(f'prov4wfs_{self._id}.json', 'w') as f:
            prov_to_json = json.dump(prov_dict, f, indent=4)
        return prov_to_json



#     from datamodel.node import Node
# from datamodel.data import Data
# from datamodel.task import Task
# import prov.model as prov
# import json

# #------------------WORKFLOW------------------–# 
# class Workflow(Node):
#     def __init__(self, id: str, name: str):
#         super().__init__(id, name)
#         self._inputs = []
#         self._outputs = []
#         self._tasks = []
#         #self._tasks = {}

#     def add_input(self, data: Data):
#         data.set_consumer(self)
#         if data.is_input():
#             self._inputs.append(data)

#     def add_output(self, data: Data):
#         data.set_producer(self)
#         if data.is_output():
#            self._outputs.append(data)

#     def add_task(self, task: 'Task'):
#         if self._tasks:
#             last_task_id = list(self._tasks.keys())[-1]
#             last_task = self._tasks[last_task_id]
#             last_task.set_target(task)
#             task.set_source(last_task)
#         self._tasks[task._id] = task

#     def to_prov(self):
#         doc = prov.ProvDocument()
#         doc.set_default_namespace('http://anotherexample.org/')
#         #to add namespaces
#         #doc.add_namespace("dcterms", "http://purl.org/dc/terms/")
#         #TODO: is it a valid anamespace??
#         doc.add_namespace('prov4wfs', 'http://placeholder.org/prov4wfs') 

#         # Add workflow as activity
#         # doc.activity(identifier, startTime=None, endTime=None, other_attributes=None)
#         doc.activity(self._id, self._start_time, self._end_time, {
#             'prov:label': self._name,
#             'prov:type': 'prov:Activity'#,
#             # 'prov4wfs:input': json.dumps([input.to_dict() for input in self._inputs]),
#             # 'prov4wfs:output': json.dumps([output.to_dict() for output in self._outputs]),
#             # 'prov4wfs:tasks': json.dumps([task.to_dict() for task in self._tasks.values()])
#         })
#         # Add tasks as activities and enactors as agents
#         for task in self._tasks:
#             doc.activity(task._id, task._start_time, task._end_time, {
#                 'prov:label': task._name,
#                 'prov:type': 'prov:Activity' #,
#                 # 'prov4wfs:input': json.dumps(task._inputs),
#                 # 'prov4wfs:output': json.dumps(task._outputs),
#                 # 'prov4wfs:source': json.dumps(task._source),
#                 # 'prov4wfs:target': json.dumps(task._target)
#                 })
#             if task._enactor is not None:
#                 doc.agent(task._enactor._id, {
#                     'prov:label': task._enactor._name,
#                     'prov:type': 'prov:Agent'
#                 })

#                 # Add wasAttributedTo relations between enactor and data items
#                 for data_item in task._enactor._attributed_to:
#                     doc.entity(data_item._id, {
#                         'prov:label': data_item._name,
#                         'prov:type': 'prov:Entity',
#                         'prov4wfs:producer': data_item._producer, 
#                         'prov4wfs:consumer': data_item._consumer
#                     })
#                     doc.wasAttributedTo(data_item._id, task._enactor._id)

#                 # Add actedOnBehalfOf relations between enactor and the enactors it acted for
#                 if task._enactor._acted_for is not None:
#                     doc.agent(task._enactor._acted_for._id, {
#                         'prov:label': task._enactor._acted_for._name,
#                         'prov:type': 'prov:Agent'
#                     })
#                     doc.actedOnBehalfOf(task._enactor._id, task._enactor._acted_for._id)

#                 # Add wasAssociatedWith relation between task and enactor
#                 doc.wasAssociatedWith(task._id, task._enactor._id)

#             # Add wasInformedBy relation between tasks
#             if task._source is not None:
#                 doc.wasInformedBy(task._id, task._source._id)

#             # Add used and wasGeneratedBy relations for inputs and outputs
#             for data_item in task._inputs:
#                 doc.entity(data_item._id, {
#                         'prov:label': data_item._name,
#                         'prov:type': 'prov:Entity',
#                         'prov4wfs:producer': data_item._producer, 
#                         'prov4wfs:consumer': data_item._consumer
#                 })
#                 doc.used(task._id, data_item._id)
#             for data_item in task._outputs:
#                 doc.entity(data_item._id, {
#                         'prov:label': data_item._name,
#                         'prov:type': 'prov:Entity',
#                         'prov4wfs:producer': data_item._producer, 
#                         'prov4wfs:consumer': data_item._consumer
#                 })
#             doc.wasGeneratedBy(data_item._id, task._id)

#         return doc.serialize(format='json')
    