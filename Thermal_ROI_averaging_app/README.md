# Thermal ROI Averaging Application

## Overview

- This Python application processes infrared thermal videos and 
  calculates temperature changes within user-defined regions of 
  interest (ROIs).

- The application was developed for analyzing thermal response 
  during laser ablation experiments and recognizes RGB format.

## Features

- Import FLIR thermal videos
- Define user-selected regions of interest (ROIs)
- Calculate average temperature within selected regions
- Remove unwanted background regions
- Export temperature data for further analysis

## Requirements

Python version:
- Python 3.x

Required packages:

```bash
pip install numpy opencv-python matplotlib pandas
```

## Known Bugs

- This application has a current bug that doesn't allow for auto
  detection of RGB calibration for auto detection of infrared color 
  bar limits. Manual calibration is mandatory if auto detection is
  set on FLIR camera or thermal camera. If color bar limits stay
  consent, then code works as expected.

