from setuptools import find_packages, setup


setup(
    name="sshkit",
    version="0.1.6",
    packages=find_packages("src"),
    package_dir={"": "src"},
)
