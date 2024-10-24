from setuptools import setup, find_packages

def parse_requirements(filename):
    with open(filename, 'r') as file:
        return [line.strip() for line in file if line.strip() and not line.startswith('#')]

setup(
    name='yprov4wfs',                    
    version='0.1.0',                     
    packages=find_packages(),           
    install_requires=parse_requirements('requirements.txt'),
    author='Carolina Sopranzetti',
    author_email='',                  
    description='A module for tracking the provenance of a workflow using a Workflow Management System.',  
    long_description=open('README.md').read(), 
    long_description_content_type='text/markdown',  
    url='https://github.com/HPCI-Lab/yProv4WFs',
    license='GNU General Public License v3 (GPLv3)',  
    classifiers=[                        
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',  
        'Operating System :: OS Independent',      
    ],
    python_requires='>=3.6',
    maintainer='HPCI Lab - University of Trento',             
)
