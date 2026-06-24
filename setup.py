from setuptools import find_packages, setup

with open("requirements.txt") as f:
    requirements = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
    name="gege_hr",
    version="0.1.0",
    description="HR Portal (gege_hr) — Vietnamese flexible-shift attendance & payroll on Frappe.",
    author="Gege",
    author_email="dev@gege.local",
    url="https://github.com/gege/gege_hr",
    packages=find_packages(),
    include_package_data=True,
    install_requires=requirements,
    python_requires=">=3.9",
    zip_safe=False,
)
