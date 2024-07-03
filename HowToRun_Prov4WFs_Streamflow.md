## PROV4WFs - StreamFlow

Necessary steps to work with Prov4WFs in StreamFlow:

- Clone StreamFlow repository
```bash
git clone https://github.com/alpha-unito/streamflow.git
```
 
<!--
- Add StreamFlow folder into your own project
-->

- Follow StreamFlow documentation for running your project using Docker or Kubernetes https://github.com/alpha-unito/streamflow/blob/master/README.md

- Go into StreamFlow folder locally and modify file /streamflow/provenance/__init__.py
  Completely delete the content and replace with the following code:

  ```bash
  from prov4wfs.prov4wfs_Streamflow_fromdb import PROW4WFSProvenanceManager
  
  prov_classes = {"run_crate": {"cwl": PROW4WFSProvenanceManager}}
  ```

- Create your workflow following StreamFlow instructions
  (Example: https://github.com/alpha-unito/streamflow/tree/master/examples/flux)

- First run the workflow using
  ```bash
  streamflow run streamflow.yml
  ```

  then use streamflow prov commad to track the provenance using our service
  ```bash
  streamflow prov <"workflow_id">
  ```

  For more information on how to get the workflow_id in StreamFlow follow this link
  https://github.com/HPCI-Lab/prov4wfs/blob/main/HowToGet_StreamFlow_workflowID.md

- The expected output is provided as a zip file in which to find the tracked provenance as a json file.
