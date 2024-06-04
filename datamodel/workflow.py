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
        self._num_tasks = None
        self._tasks_done = None
        self._tasks_failed = None
        self._taks_skipped = None
        self._type = None
        self._engineWMS = None
        self._resource_cwl_uri = None
        
    def add_input(self, data: Data):
        data.set_consumer(self)
        if data.is_input:
            self._inputs.append(data)
            
    def add_output(self, data: Data):
        data.set_producer(self)
        if data.is_output:
            self._outputs.append(data)
            
    def add_task(self, task: 'Task'): 
        if self._tasks:
            last_task = self._tasks[-1]
            last_task.set_next(task)
            task.set_prev(last_task)
        self._tasks.append(task)
        
    def get_task_by_id(self, id):
        for task in self._tasks:
            if task.id == id:
                return task
        return None

    
    
    def to_prov(self):
        doc = prov.ProvDocument()
        doc.set_default_namespace('http://anotherexample.org/')
        doc.add_namespace('prov4wfs', 'http://example.org')
        
        if self._resource_cwl_uri is not None:
            doc.activity(self._id, self._start_time, self._end_time,{
                'prov:label': self._name,
                'prov:type': 'prov:Activity',
                'prov4wfs:level': self._level, 
                'prov4wfs:engine': self._engineWMS,
                'prov4wfs:status': self._status,
                'prov4wfs:resource_uri': self._resource_cwl_uri,
            })
        
        for input in self._inputs:
            if input is not None:
                doc.entity(input._id, {
                        'prov:label': input._name,
                        'prov:type': 'prov:Entity'
                })
                doc.used(self._id, input._id)
        for output in self._outputs:
            if output is not None:
                doc.entity(output._id, {
                        'prov:label': output._name,
                        'prov:type': 'prov:Entity'
                })
                doc.wasGeneratedBy(output._id, self._id)
                
        # Add tasks as activities and agents as agents
        for task in self._tasks:
            doc.activity(task._id, task._start_time, task._end_time, {
                'prov:label': task._name,
                'prov:type': 'prov:Activity',
                'prov4wfs:status': task._status,
                'prov4wfs:level': task._level,
                })
            # Add wasStartedBy relation between task and workflow
            # doc.wasStartedBy(task._id, self._id, None)
            
            if task._agent is not None:
                doc.agent(task._agent._id, {
                    'prov:label': task._agent._name,
                    'prov:type': 'prov:Agent'
                })
                # Add wasAttributedTo relations between agent and data items
                for data_item in task._agent._attributed_to:
                    if data_item is not None: 
                        doc.entity(data_item._id, {
                            'prov:label': data_item._name,
                            'prov:type': 'prov:Entity'
                        })
                        doc.wasAttributedTo(data_item._id, task._agent._id)
                
                # Add actedOnBehalfOf relations between agent and the agents it acted for
                if task._agent._acted_for is not None:
                    doc.agent(task._agent._acted_for._id, {
                        'prov:label': task._agent._acted_for._name,
                        'prov:type': 'prov:Agent'
                    })
                    doc.actedOnBehalfOf(task._agent._id, task._agent._acted_for._id)

                # Add wasAssociatedWith relation between task and agent
                doc.wasAssociatedWith(task._id, task._agent._id)

                      
            # Add used and wasGeneratedBy relations for inputs and outputs
            for data_item in task._inputs:
                if data_item is not None:
                    doc.entity(data_item._id, {
                            'prov:label': data_item._name,
                            'prov:type': 'prov:Entity'
                    })
                    doc.used(task._id, data_item._id)
            for data_item in task._outputs:
                if data_item is not None:
                    doc.entity(data_item._id, {
                            'prov:label': data_item._name,
                            'prov:type': 'prov:Entity'
                    })
                    # doc.wasGeneratedBy(data_item._id, task._id)
                    doc.wasGeneratedBy(data_item._id, task._id)

                
            # Add wasInformedBy relation between tasks
            if task._prev is not None:
                # doc.wasInformedBy(task._id, task._prev._id)
                doc.wasInformedBy(task._id, task._prev._id)

        return doc.serialize(format='json')
    
    def prov_to_json(self):
        prov_dict = json.loads(self.to_prov())
        json_file_path = f'prov4wfs_{self._id}.json'
        with open(json_file_path, 'w') as f:
            json.dump(prov_dict, f, indent=4)
        return json_file_path
