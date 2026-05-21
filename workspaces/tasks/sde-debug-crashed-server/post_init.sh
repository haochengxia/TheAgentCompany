#!/bin/bash
set -e
cd /workspace && zip -r app.zip app/ -P 2039fome && rm -rf /workspace/app/
