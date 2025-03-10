## yProv4WFs - StreamFlow
The yProv4WFs was developed for StreamFlow version 0.2.0.dev11

Necessary steps to work with yProv4WFs in StreamFlow:

- Clone StreamFlow repository
```bash
git clone https://github.com/alpha-unito/streamflow.git
```
1. Go into your StreamFlow folder locally:
* ```bash
   cd /<dir_path>/
  ```
2. Modify file /streamflow/provenance/__init__.py via text editor (es. nano, vim):
  * ```bash
     nano streamflow/provenance/__init__.py
   ```
  - Replace the code inside the file with the following one:
   * ```bash
     from yprov4wfs.yProv4WFs_Streamflow.yprov4wfs_Streamflow_fromdb import yProv4WFsProvenanceManager

     prov_classes = {"run_crate": {"cwl": yProv4WFsProvenanceManager}}
     ```

  <!--
    from yProv4wfs.yprov4wfs_Streamflow_fromdb import yProv4WFsProvenanceManager
  -->
    
  
- Using yProv4WFs library:
  1. Installing using pip:
    * ```bash
      pip install yprov4wfs
      ```
    * verify installation: 
      ```bash
      pip show yprov4wfs
      ```
  2. Installing by cloning the repository:
    * ```bash
      git clone https://github.com/HPCI-Lab/yProv4WFs.git
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
  
  N.B : If you are working inside a Docker environment be sure to install yProv4WFs library via:
    * ```bash
      pip install yprov4wfs
      ```
    * or defining the installation in the DockerFile
<!--
- Add StreamFlow folder into your own project
-->

- Follow [StreamFlow documentation](https://github.com/alpha-unito/streamflow/blob/master/README.md) for running your project using Docker or Kubernetes 

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
