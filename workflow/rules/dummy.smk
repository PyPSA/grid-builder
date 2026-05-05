rule dummy_add_text:
    input:
        user_message="<user_message>",
        readme="<resources>/automatic/dummy_readme.md",
    output:
        combined_text="<combined_text>",
    log:
        "<logs>/dummy_add_text.log",
    conda:
        "../envs/shell.yaml"
    params:
        config_text=config["dummy_text"],
    message:
        "Dummy rule combining user inputs and automatic downloads."
    script:
        "../scripts/dummy_script.py"
