# IPG Graphical User Interface (GUI) for Duration Parameter Setting

- This folder contains the neccessary python and html for the IPG TFL laser duration

- The user must have purchased an IPG photonics thulium fiber laser (TFL)
  and also be connected to that laser in order to use this interface.

- This GUI has two interfaces, the first will be a duplicate of the
  IPG default interface while the second will be a duration GUI
  allowing the user to specifiy in milliseconds (ms) the duration
  the laser is emmitted with a 3 second delay to account for the 
  default delay in the built in system of the IPG system. 
  (change duration as needed)

## Requirements

Python 3.10+

Required Python packages:

```bash
pip install -r requirements.txt
```

## Known Bugs

- None known of at this time