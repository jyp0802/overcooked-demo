#!/bin/sh
sudo -v
mkdir app

cp server/requirements.txt app/
pip install -r app/requirements.txt
pip install eventlet

git clone https://github.com/jyp0802/overcooked_ai.git --branch modularize --single-branch app/overcooked_ai
git clone https://github.com/jyp0802/human_aware_rl.git --branch master --single-branch app/human_aware_rl

echo "import os; DATA_DIR=os.path.abspath('.')" >> app/human_aware_rl/human_aware_rl/data_dir.py
pip install -e app/overcooked_ai
pip install -e app/human_aware_rl

sudo apt-get update
sudo apt-get install -y libgl1-mesa-dev

cp -r server/static app/
cp -r server/*.py app/
cp -r server/graphics/overcooked_graphics_v2.2.js app/static/js/graphics.js
cp -r server/config.json app/