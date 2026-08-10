#!/bin/bash

R='\e[91m'
G='\e[92m'
Y='\e[93m'
B='\e[94m'
M='\e[95m'
C='\e[96m'
W='\e[97m'
NC='\e[0m'

password=$(</home/holuser/Desktop/PASSWORD.txt)
remote_hosts="hosts.txt"

remote_user="holuser"
ssh_options="-n -q -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o LogLevel=ERROR"
scp_options="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes -o LogLevel=ERROR"
source_files=(
    /home/holuser/Documents/files/hol-2711-06/workbench_prep.sh
    /home/holuser/Documents/files/hol-2711-06/workbench_end.sh
)

remote_folder="/home/holuser"

if [ -z "$password" ]; then
    echo -e "Error: Password is empty. Please ensure PASSWORD.txt contains the correct password."
    exit 1
fi

[[ -f "$remote_hosts" ]] || { echo -e "$remote_hosts file not found!"; exit 1; }

for file in "${source_files[@]}"; do
    [[ -f "$file" ]] || { echo -e "$file file not found!"; exit 1; }
done

while IFS= read -r host || [[ -n "$host" ]]; do

    [[ -z "$host" || "$host" =~ ^# ]] && continue

    echo -e "${B}Processing host: ${C}${host}${NC}"
    for file in "${source_files[@]}"; do
        remote_file="${remote_folder}/${file##*/}"
        sshpass -p "$password" scp "$file" "${remote_user}@${host}:$remote_file" && echo -e "${G}File: $file copied successfully to ${host}:${remote_file}${NC}" || { echo -e "${R}Failed to copy file: $file to ${host}:${remote_file}${NC}"; continue; }
    done

done < "$remote_hosts"
