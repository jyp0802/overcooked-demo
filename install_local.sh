#!/bin/sh

sudo -v

if [ -d "app" ];
then
    rm -rf app
fi
mkdir app

cp server/requirements.txt app/
pip install -r app/requirements.txt
pip install eventlet

sudo apt-get update
sudo apt-get install -y libgl1-mesa-dev

echo "import os; DATA_DIR=os.path.abspath('.')" >> ../human_aware_rl/human_aware_rl/data_dir.py

cp -r server/static app/
cp -r server/*.py app/
cp -r server/graphics/overcooked_graphics_v2.2.js app/static/js/graphics.js
cp -r server/graphics/,my_config.json app/static/js/my_config.json
cp -r server/config.json app/