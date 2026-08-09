# CLAUDE.md — ReClip Telegram Bot

Read and follow the project-wide instructions in `AGENTS.md` before working in
this repository.

## Deployment

Production deployments are performed through the Ansible repository at
`/Users/tabolin/projects/orangepi-ansible`, where the deployment playbooks,
inventory, and related configuration live. Do not deploy directly from this
repository. After publishing a ReClip release and its container images, switch
to `orangepi-ansible`, follow its repository instructions, update the image
version through its existing variables, run the appropriate playbook, and
verify the deployed containers and service health.
