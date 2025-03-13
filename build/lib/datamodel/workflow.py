from datamodel.node import Node
from datamodel.data import Data
from datamodel.task import Task

from pathlib import Path
from uuid import uuid4
import traceback
import json
import os

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
            
    def add_task(self, task: Task): 
        self._tasks.append(task)
        
    def get_task_by_id(self, id):
        for task in self._tasks:
            if task.id == id:
                return task
        return None

    # to_prov function without dependences to prov.model library
    def to_prov(self):
        try:
            doc = {
                'prefix': {
                    'default': 'http://anotherexample.org/',
                    'yprov4wfs': 'http://example.org'
                },
                'activity': {},
                'entity': {},
                'agent': {},
                'used': {},
                'wasGeneratedBy': {},
                'wasAssociatedWith': {},
                'wasAttributedTo': {},
                'actedOnBehalfOf': {},
                'wasInformedBy': {}
            }

            doc['activity'][self._id] = {
                'prov:startTime': self._start_time,
                'prov:endTime': self._end_time,
                'prov:label': self._name,
                'prov:type': 'prov:Activity',
                'yprov4wfs:level': self._level,
                'yprov4wfs:engine': self._engineWMS,
                'yprov4wfs:status': self._status,
            }
            if self._resource_cwl_uri is not None:
                doc['activity'][self._id]['yprov4wfs:resource_uri'] = self._resource_cwl_uri

            for input in self._inputs:
                if input is not None:
                    doc['entity'][input._id] = {
                        'prov:label': input._name,
                        'prov:type': 'prov:Entity'
                    }
                    doc['used'][f'{str(uuid4())}'] = {'prov:activity': self._id, 'prov:entity': input._id}

            for output in self._outputs:
                if output is not None:
                    doc['entity'][output._id] = {
                        'prov:label': output._name,
                        'prov:type': 'prov:Entity'
                    }
                    doc['wasGeneratedBy'][f'{str(uuid4())}'] = {'prov:entity': output._id, 'prov:activity': self._id}
            
            for task in self._tasks:
                if task is not None:
                    doc['activity'][task._id] = {
                        'prov:startTime': task._start_time,
                        'prov:endTime': task._end_time,
                        'prov:label': task._name,
                        'prov:type': 'prov:Activity',
                        'yprov4wfs:status': task._status,
                        'yprov4wfs:level': task._level
                    }

                    if task._agent is not None:
                        doc['agent'][task._agent._id] = {
                            'prov:label': task._agent._name,
                            'prov:type': 'prov:Agent'
                        }
                        for data_item in task._agent._attributed_to:
                            if data_item is not None:
                                doc['entity'][data_item._id] = {
                                    'prov:label': data_item._name,
                                    'prov:type': 'prov:Entity'
                                }
                                doc['wasAttributedTo'][f'{str(uuid4())}'] = {'prov:entity': data_item._id, 'prov:agent': task._agent._id}

                        if task._agent._acted_for is not None:
                            doc['agent'][task._agent._acted_for._id] = {
                                'prov:label': task._agent._acted_for._name,
                                'prov:type': 'prov:Agent'
                            }
                            doc['actedOnBehalfOf'][f'{str(uuid4())}'] = {'prov:delegate': task._agent._id, 'prov:responsible': task._agent._acted_for._id}

                        doc['wasAssociatedWith'][f'{str(uuid4())}'] = {'prov:activity': task._id, 'prov:agent': task._agent._id}

                    for data_item in task._inputs:
                        if data_item is not None:
                            doc['entity'][data_item._id] = {
                                'prov:label': data_item._name,
                                'prov:type': 'prov:Entity'
                            }
                            doc['used'][f'{str(uuid4())}'] = {'prov:activity': task._id, 'prov:entity': data_item._id}
                    for data_item in task._outputs:
                        if data_item is not None:
                            doc['entity'][data_item._id] = {
                                'prov:label': data_item._name,
                                'prov:type': 'prov:Entity'
                            }
                        doc['wasGeneratedBy'][f'{str(uuid4())}'] = {'prov:entity': data_item._id, 'prov:activity': task._id}

                    if task._prev is not None:
                        for prev_task in task._prev:
                            doc['wasInformedBy'][f'{str(uuid4())}'] = {'prov:informed': task._id, 'prov:informant': prev_task._id}
                            
            # Helper function to remove empty lists from the dictionary
            def remove_empty_lists(d):
                if isinstance(d, dict):
                    return {k: remove_empty_lists(v) for k, v in d.items() if v != []}
                elif isinstance(d, list):
                    return [remove_empty_lists(i) for i in d if i != []]
                else:
                    return d

            # Remove empty lists from the document
            doc = remove_empty_lists(doc)
            
            def convert(obj):
                if isinstance(obj, Path):
                    return str(obj)
                raise TypeError("Object of type '%s' is not JSON serializable" % type(obj).__name__)
            
            return json.dumps(doc, indent=4, default=convert)
        except Exception as e:
            print(f"Error: {e} ")
            traceback.print_exc()
            return None

  
    def prov_to_json(self, directory_path=None):
        try:
            if directory_path is None:
                prov_json = self.to_prov()
                if prov_json is None:
                    raise ValueError("Failed to serialize the document to JSON.")
                json_file_path = f'yprov4wfs.json'
            else:
                os.makedirs(directory_path, exist_ok=True)
                prov_json = self.to_prov()
                if prov_json is None:
                    raise ValueError("Failed to serialize the document to JSON.")
                json_file_path = os.path.join(directory_path,f'yprov4wfs.json')

            with open(json_file_path, 'w') as f:
                f.write(prov_json)
            return json_file_path

        except Exception as e:
            print(f"Error: {e} ")
            traceback.print_exc()
            return None

# to_prov function with dependences to prov.model library
# import prov.model as prov   
    # def to_prov(self):
    #     doc = prov.ProvDocument()
    #     doc.set_default_namespace('http://anotherexample.org/')
    #     doc.add_namespace('prov4wfs', 'http://example.org')
        
    #     if self._resource_cwl_uri is not None:
    #         doc.activity(self._id, self._start_time, self._end_time,{
    #             'prov:label': self._name,
    #             'prov:type': 'prov:Activity',
    #             'prov4wfs:level': self._level, 
    #             'prov4wfs:engine': self._engineWMS,
    #             'prov4wfs:status': self._status,
    #             'prov4wfs:resource_uri': self._resource_cwl_uri,
    #         })
        
    #     for input in self._inputs:
    #         if input is not None:
    #             doc.entity(input._id, {
    #                     'prov:label': input._name,
    #                     'prov:type': 'prov:Entity'
    #             })
    #             doc.used(self._id, input._id)
    #     for output in self._outputs:
    #         if output is not None:
    #             doc.entity(output._id, {
    #                     'prov:label': output._name,
    #                     'prov:type': 'prov:Entity'
    #             })
    #             doc.wasGeneratedBy(output._id, self._id)
                
    #     # Add tasks as activities and agents as agents
    #     for task in self._tasks:
    #         doc.activity(task._id, task._start_time, task._end_time, {
    #             'prov:label': task._name,
    #             'prov:type': 'prov:Activity',
    #             'prov4wfs:status': task._status,
    #             'prov4wfs:level': task._level,
    #             })
    #         # Add wasStartedBy relation between task and workflow
    #         # doc.wasStartedBy(task._id, self._id, None)
            
    #         if task._agent is not None:
    #             doc.agent(task._agent._id, {
    #                 'prov:label': task._agent._name,
    #                 'prov:type': 'prov:Agent'
    #             })
    #             # Add wasAttributedTo relations between agent and data items
    #             for data_item in task._agent._attributed_to:
    #                 if data_item is not None: 
    #                     doc.entity(data_item._id, {
    #                         'prov:label': data_item._name,
    #                         'prov:type': 'prov:Entity'
    #                     })
    #                     doc.wasAttributedTo(data_item._id, task._agent._id)
                
    #             # Add actedOnBehalfOf relations between agent and the agents it acted for
    #             if task._agent._acted_for is not None:
    #                 doc.agent(task._agent._acted_for._id, {
    #                     'prov:label': task._agent._acted_for._name,
    #                     'prov:type': 'prov:Agent'
    #                 })
    #                 doc.actedOnBehalfOf(task._agent._id, task._agent._acted_for._id)

    #             # Add wasAssociatedWith relation between task and agent
    #             doc.wasAssociatedWith(task._id, task._agent._id)

                      
    #         # Add used and wasGeneratedBy relations for inputs and outputs
    #         for data_item in task._inputs:
    #             if data_item is not None:
    #                 doc.entity(data_item._id, {
    #                         'prov:label': data_item._name,
    #                         'prov:type': 'prov:Entity'
    #                 })
    #                 doc.used(task._id, data_item._id)
    #         for data_item in task._outputs:
    #             if data_item is not None:
    #                 doc.entity(data_item._id, {
    #                         'prov:label': data_item._name,
    #                         'prov:type': 'prov:Entity'
    #                 })
    #                 # doc.wasGeneratedBy(data_item._id, task._id)
    #                 doc.wasGeneratedBy(data_item._id, task._id)

                
    #         # Add wasInformedBy relation between tasks
    #         if task._prev is not None:
    #             # doc.wasInformedBy(task._id, task._prev._id)
    #             doc.wasInformedBy(task._id, task._prev._id)

    #     return doc.serialize(format='json')
    
    # def prov_to_json(self):
    #     prov_dict = json.loads(self.to_prov())
    #     json_file_path = f'yprov4wfs_{self._id}.json'
    #     with open(json_file_path, 'w') as f:
    #         json.dump(prov_dict, f, indent=4)
    #     return json_file_path

