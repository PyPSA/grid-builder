"""A simple script to serve as an example.

Should be deleted in real workflows.
"""

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    snakemake: Any
sys.stderr = open(snakemake.log[0], "w")

config = snakemake.params.config_text
readme = Path(snakemake.input.readme).read_text()
user = Path(snakemake.input.user_message).read_text()

output_text = "\n\n".join([readme, user, config])

Path(snakemake.output.combined_text).write_text(output_text)
