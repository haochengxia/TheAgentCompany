#!/bin/bash
set -e
curl -f -S -o /utils/reference.jpg https://images.pexels.com/photos/27220813/pexels-photo-27220813.jpeg
[ -s /utils/reference.jpg ] || (echo "Failed to download image" && exit 1)
