"""Build into a staging dir instead of the live output/ so a crash can't
take the served site down. Run with the project venv python."""
import asyncio
from pathlib import Path
import generate

generate.OUTPUT_DIR = Path(__file__).parent / "output_staging"
asyncio.run(generate.main())
