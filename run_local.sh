#!/bin/sh
cp -r server/static app/
cp -r server/*.py app/
cp -r server/graphics/overcooked_graphics_v2.2.js app/static/js/graphics.js
cp -r server/graphics/my_config.json app/static/js/my_config.json
cp -r server/config.json app/


cd app
python app.py
cd ..