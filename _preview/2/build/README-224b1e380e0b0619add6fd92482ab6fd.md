# [OpenScienceLab Cookbook Template] Replace with Your Title

<img src="assets/ASF_logo.svg" alt="thumbnail" width="300"/>

[![nightly-build](https://github.com/ASFOpenSARlab/ASF_Cookbook_Template/actions/workflows/nightly-build.yaml/badge.svg)](https://github.com/ASFOpenSARlab/ASF_Cookbook_Template/actions/workflows/nightly-build.yaml)


This Cookbook Template was adapted from the Project Pythia [Cookbook Template](https://github.com/projectpythia/cookbook-template/blob/main/README.md). It has been updated to provide templates for ASF Cookbooks using the Pixi package manager.

See the [Project Pythia Cookbook Contributor's Guide](https://projectpythia.org/cookbook-guide/#:~:text=forking%20workflow.-,G.%20Deploying%20your%20Cookbook,%C2%B6,-Pythia%20Cookbooks%20are) for instructions on deploying your Cookbook to GitHub Pages.

This Cookbook covers ... (replace `...` with the main subject of your cookbook ... e.g., _working with radar data in Python_)

## Motivation

(Add a few sentences stating why this cookbook will be useful. What skills will you, "the chef", gain once you have reached the end of the cookbook?)

## Authors

First Author, Second Author, etc. _Acknowledge primary content authors here! You can include links to their GitHub profiles or other unique pages._

### Contributors

<a href="https://github.com/ASFOpenSARlab/ASF_Cookbook_Template/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=ASFOpenSARlab/ASF_Cookbook_Template" />
</a>

## Structure

(State one or more sections that will comprise the notebook. E.g., _This cookbook is broken up into two main sections - "Foundations" and "Example Workflows."_ Then, describe each section below.)

### Section 1 ( Replace with the title of this section, e.g. "Foundations" )

(Add content for this section, e.g., "The foundational content includes ... ")

### Section 2 ( Replace with the title of this section, e.g. "Example workflows" )

(Add content for this section, e.g., "Example workflows include ... ")

## Running the Notebooks

### Running in a Jupyter Hub

If you are working in a Jupyter Hub that allows you to build Pixi environments, such as [OpenSARLab](https://opensarlab-docs.asf.alaska.edu/user-guides/opensarlab/), use the following steps to run the notebooks in this cookbook.

1. Clone the repository:

   ```bash
    git clone https://github.com/your_account/your_cookbook.git
   ```

1. Run the `notebooks/software_environment.ipynb` notebook.

### Running on Your Own Machine

If you are interested in running this material locally on your computer, you will need to follow this workflow:

(Replace "your_account/your_cookbook" with the GitHub org or user name and title of your cookbook repository)

1. Create a copy of this Cookbook template repository, by clicking the `Use this template button` and selecting the `Create a new repository` option on this [repository's GitHub page](). 

1. Clone the new repository:

   ```bash
    git clone https://github.com/your_account/your_cookbook.git
   ```

1. Move into the `your_cookbook` directory
   ```bash
   cd your_cookbook
   ```
1. Launch in Jupyter Lab with the included Pixi environment
    ```bash
    pix run lab
    ```
