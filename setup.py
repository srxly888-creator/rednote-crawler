from setuptools import setup, find_packages

setup(
    name="xhs-drission-crawler",
    version="0.1.0",
    description="A resilient Xiaohongshu (RedNote) crawler based on DrissionPage.",
    author="Your Name",
    author_email="yoursimulated@email.com",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "DrissionPage>=4.0",
        "loguru",
        "tenacity",
        "requests",
    ],
    extras_require={
        "images": ["Pillow"],
    },
)
