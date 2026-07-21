from flask import Flask, request, jsonify, render_template
from selenium import webdriver
from selenium.webdriver.common.by import By
# from selenium.webdriver.chrome.service import Service

import time
import threading

app = Flask(__name__)

LASER_PAGE = "http://192.168.3.230" # add your specified ip address for the IPG laser

driver = webdriver.Chrome()

driver.get(LASER_PAGE)

time.sleep(3)

stop_flag = False


@app.route("/")
def home():
    return render_template("index.html")

@app.route("/fire_laser", methods=["POST"])
def fire_laser():

    duration_ms = int(request.json["duration"])
    duration_s = duration_ms / 1000

    try:

        button = driver.find_element(By.ID, "ENEMS")

        # Turn laser on
        button.click()

        # Laser warmup
        time.sleep(3)

        # emission time
        time.sleep(duration_s)

        # Turn laser off
        button.click()

        return jsonify({"status": "complete"})
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    
@app.route("/stop_laser", methods=["POST"])
def stop_laser():

    global stop_flag
    stop_flag = True

    return jsonify({"status": "stopping"})

if __name__ == "__main__":
    app.run(port=5000)