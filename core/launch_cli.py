"""Parse launcher options before resolving the instance directory."""

import argparse


def parse_args():
    parser = argparse.ArgumentParser(description='Run Altera from its configured instance directory.')
    parser.add_argument('--dev', action='store_true', help='Use Flask development mode, without an automatic reloader.')
    parser.add_argument('--open', action='store_true', help='Open the browser once the server is reachable.')
    return parser.parse_args()
