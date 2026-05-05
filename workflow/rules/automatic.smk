"""Rules to used to download automatic resource files."""


rule dummy_download:
    output:
        readme="<resources>/automatic/dummy_readme.md",
    log:
        "<logs>/dummy_download.log",
    conda:
        "../envs/shell.yaml"
    params:
        url=internal["resources"]["automatic"]["dummy_readme"],
    message:
        "Download the Modelblocks README file."
    shell:
        'curl -sSLo {output.readme} "{params.url}"'
