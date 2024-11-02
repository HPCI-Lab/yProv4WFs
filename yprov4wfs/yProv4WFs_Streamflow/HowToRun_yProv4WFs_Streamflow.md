## yProv4WFs - StreamFlow

Necessary steps to work with yProv4WFs in StreamFlow:

- Clone StreamFlow repository
```bash
git clone https://github.com/alpha-unito/streamflow.git
```
 
- Using yProv4WFs library:
  1. Installing using pip:
    * ```bash
      pip install yprov4wfs
      ```
    * verify installation: 
      ```bash
      pip show yprov4wfs
      ```
  2. Installing by downloading the repository:
    * ```bash
      cd /path/to/your/project
      ```
    * ```bash
      pip install .
      ```
  3. Installing from a GitHub Repository:
    * ```bash
      pip install git+https://github.com/HPCI-Lab/yProv4WFs.git
      ```
    * verify installation: 
      ```bash
      pip show yprov4wfs
      ```
<!--
- Add StreamFlow folder into your own project
-->

- Follow [StreamFlow documentation](https://github.com/alpha-unito/streamflow/blob/master/README.md) for running your project using Docker or Kubernetes 

- Go into StreamFlow folder locally and modify file /streamflow/provenance/__init__.py
  Completely delete the content and replace with the following code:

  ```bash
<!--
  from yProv4wfs.yprov4wfs_Streamflow_fromdb import yProv4WFsProvenanceManager
-->
  from yprov4wfs.yProv4WFs_Streamflow.yprov4wfs_Streamflow_fromdb import yProv4WFsProvenanceManager

  prov_classes = {"run_crate": {"cwl": yProv4WFsProvenanceManager}}
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

  For more information on how to get the workflow_id in StreamFlow follow this
  [link](https://github.com/HPCI-Lab/yProv4WFs/blob/main/yprov4wfs/yProv4WFs_Streamflow/HowToGet_StreamFlow_workflowID.md)

- The expected output is provided as a zip file in which to find the tracked provenance as a json file.
