# This code is part of Tergite
#
# (C) Copyright Miroslav Dobsicek 2020, 2021
# (C) Copyright David Wahlstedt 2023
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.


from setuptools import find_packages, setup

REQUIREMENTS = [
    "aiofiles>=0.7.0",
    "fastapi>=0.65.1",
    "motor>=2.4.0",
    "python-multipart>=0.0.5",
    "redis>=3.5.3",
    "requests>=2.25.1",
    "rq>=1.10.0",
    "resonator-tools-vdrhtc>=0.12",
    "uvicorn>=0.13.4",
    "numpy==1.23.5",
    "PyQt5>=5.15.4",
    "h5py>=3.2.1",
    "scipy>=1.8.0",
    "networkx>=2.5.1",
    "syncer>=1.3.0",
    "tqcsf>=1.0.0",
    "matplotlib>=3.5.1",
    "toml>=0.10.2",
    "scikit-learn==1.1.3",
]

setup(
    name="tergite-bcc",
    author_emails="dobsicek@chalmers.se",
    license="Apache 2.0",
    packages=find_packages(),
    install_requires=REQUIREMENTS,
    python_requires=">=3.8",
)
