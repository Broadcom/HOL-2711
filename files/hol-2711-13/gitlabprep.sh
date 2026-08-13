#!/bin/bash

# ==========================================
# Configuration Variables
# ==========================================
# Replace with your GitLab instance URL (e.g., https://gitlab.com or your self-hosted URL)
GITLAB_URL="https://gitlab.vcf.lab"

# Replace with your Admin Personal Access Token
PRIVATE_TOKEN="<changeme>"

# ==========================================

# Check if jq is installed
if ! command -v jq &> /dev/null; then
    echo "Error: jq is not installed. Please install jq to run this script."
    exit 1
fi

# Function to create a group, project, and commits
setup_environment() {
    local group_name=$1
    local project_name=$2
    local new_branch_name="feature-setup"

    echo "------------------------------------------------"
    echo "Setting up Group: $group_name | Project: $project_name"
    echo "------------------------------------------------"

    # 1. Create the Public Group
    echo "Creating group '$group_name'..."
    local group_response=$(curl -sS -k -X POST "${GITLAB_URL}/api/v4/groups" \
        -H "PRIVATE-TOKEN: ${PRIVATE_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{
            \"name\": \"${group_name}\",
            \"path\": \"${group_name}\",
            \"visibility\": \"public\"
        }")

    # Extract Group ID
    local group_id=$(echo $group_response | jq -r '.id')
    
    if [ "$group_id" == "null" ] || [ -z "$group_id" ]; then
        echo "Failed to create group '$group_name'. Response:"
        echo $group_response | jq .
        return
    fi
    echo "Success: Group '$group_name' created with ID: $group_id"

    # 2. Create the Public Project assigned to the new Group
    # We use initialize_with_readme=true so a default 'main' branch is created instantly.
    echo "Creating project '$project_name' in group '$group_name'..."
    local project_response=$(curl -sS -k -X POST "${GITLAB_URL}/api/v4/projects" \
        -H "PRIVATE-TOKEN: ${PRIVATE_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{
            \"name\": \"${project_name}\",
            \"namespace_id\": ${group_id},
            \"visibility\": \"public\",
            \"initialize_with_readme\": true
        }")

    # Extract Project ID
    local project_id=$(echo $project_response | jq -r '.id')
    local default_branch=$(echo $project_response | jq -r '.default_branch')

    if [ "$project_id" == "null" ] || [ -z "$project_id" ]; then
        echo "Failed to create project '$project_name'. Response:"
        echo $project_response | jq .
        return
    fi
    echo "Success: Project '$project_name' created with ID: $project_id (Default branch: $default_branch)"

    # Wait a moment for GitLab's background worker to finish initializing the repository
    sleep 3

    # 3. Create a sample file and commit it to the default branch (main)
    echo "Committing sample file to the '$default_branch' branch..."
    curl -sS -k -o /dev/null -w "HTTP Status: %{http_code}\n" -X POST "${GITLAB_URL}/api/v4/projects/${project_id}/repository/commits" \
        -H "PRIVATE-TOKEN: ${PRIVATE_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{
            \"branch\": \"${default_branch}\",
            \"commit_message\": \"Add initial sample file to default branch\",
            \"actions\": [
                {
                    \"action\": \"create\",
                    \"file_path\": \"config_${group_name}.json\",
                    \"content\": \"{\\n  \\\"environment\\\": \\\"${group_name}\\\"\\n}\"
                }
            ]
        }"

    # 4. Create a sample file and commit it to a NEW branch
    echo "Committing a different file to a new branch '$new_branch_name'..."
    curl -sS -k -o /dev/null -w "HTTP Status: %{http_code}\n" -X POST "${GITLAB_URL}/api/v4/projects/${project_id}/repository/commits" \
        -H "PRIVATE-TOKEN: ${PRIVATE_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{
            \"branch\": \"${new_branch_name}\",
            \"start_branch\": \"${default_branch}\",
            \"commit_message\": \"Add sample file to new branch\",
            \"actions\": [
                {
                    \"action\": \"create\",
                    \"file_path\": \"feature_notes.txt\",
                    \"content\": \"This file was created in the ${new_branch_name} branch for ${project_name}.\"
                }
            ]
        }"
    echo "Done with $project_name."
    echo ""
}

# ==========================================
# Execute the setup for DEV and PROD
# ==========================================

setup_environment "hol-dev" "hol-dev-project"
setup_environment "hol-prod" "hol-prod-project"

echo "All tasks completed successfully!"