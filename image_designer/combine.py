import sys, os
from PIL import Image
import json

image_dir = "terrain"

out_dir = "assets"

image_dict = {"frames": {}}

image_width = 120
image_height = 120
border_width = 1
background_color = (255,255,255,0)

all_images = []
for filename in os.listdir(image_dir):
    all_images.append((Image.open(os.path.join(image_dir, filename)), filename))
num_images = len(all_images)

final_width = (image_width + 2*border_width) * num_images
final_height = (image_height + 2*border_width)

combined_image = Image.new("RGBA", (final_width, final_height), color=background_color)

for idx, (image, image_name) in enumerate(all_images):
    image = image.resize((image_width, image_height))
    x, y = (image_width + 2*border_width)*idx+1, 1
    combined_image.paste(image, (x, y))

    image_info = {}
    image_info["frame"] = {"x": x, "y": y, "w": image_width, "h": image_height}
    image_info["rotated"] = False
    image_info["trimmed"] = False
    image_info["spriteSourceSize"] = {"x": 0, "y": 0, "w": image_width, "h": image_height}
    image_info["sourceSize"] = {"w": image_width, "h": image_height}

    image_dict["frames"][image_name] = image_info


combined_image.save(os.path.join(out_dir, f"{image_dir}.png"), "PNG")

image_dict["meta"] = {
    "app": "http://www.codeandweb.com/texturepacker",
    "version": "1.0",
    "image": "spritesheet.png",
    "format": "RGBA8888",
    "size": {"w": final_width, "h": final_height},
    "scale": "1"
}

with open(os.path.join(out_dir, f"{image_dir}.json"), "w") as f:
    json.dump(image_dict, f, indent=4)