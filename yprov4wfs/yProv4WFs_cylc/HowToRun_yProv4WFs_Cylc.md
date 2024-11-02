## yProv4WFs - Cylc

Necessary steps to work with yProv4WFs in Cylc:

- Clone Cylc repository (inside conda environment preferably):
```bash
git clone https://github.com/cylc/cylc-flow.git
```
- Using yProv4WFs library (inside conda environment preferably):
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

- Follow Cylc documentation for running your project https://github.com/cylc/cylc-flow/blob/master/README.md

- Go into Cylc folder locally and modify file /cylc/flow/scheduler_cli.py
  Replace this import:
  ```bash
  from cylc.flow.scheduler import Scheduler, SchedulerError
  ```
  With the following code:
  ```bash
  from yprov4wfs.yProv4WFs_cylc.yprov4wfs_Cylc import Scheduler, SchedulerError
  ```

- Create your workflow following Cylc instructions <br>
  (You can find some examples in the Cylc documentation: 
  https://cylc.github.io/cylc-doc/stable/html/workflow-design-guide/index.html)

- First run the workflow using
  ```bash
  cylc vip
  ```
  or any other command line, depending on the cylc/flow version you are using.<br>
  Wait till the workflow's execution is concluded.

- The expected output is provided in the *cylc-run* directory, under the *workflow_name/run_number* folder, where you can find the tracked provenance as a json file (yProv4WFs.json).
