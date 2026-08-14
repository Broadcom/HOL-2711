#Requires -Modules VMware.VimAutomation.Core

param (
    [Parameter(
        Mandatory = $true,
        Position = 0
    )]
    [ValidateScript({
        if (-not (Test-Path $_ -PathType Leaf)) {
            throw "Configuration file does not exist: $_"
        }

        if ([System.IO.Path]::GetExtension($_) -ne ".json") {
            throw "Configuration file must be a .json file: $_"
        }

        return $true
    })]
    [string]$ConfigFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"


# ============================================================
# Logging
# ============================================================

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO]   $Message" -ForegroundColor Cyan
}

function Write-Create {
    param([string]$Message)
    Write-Host "[CREATE] $Message" -ForegroundColor Green
}

function Write-Skip {
    param([string]$Message)
    Write-Host "[SKIP]   $Message" -ForegroundColor DarkGray
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[WARN]   $Message" -ForegroundColor Yellow
}


# ============================================================
# REST Error Helpers
# ============================================================

function Get-RestErrorDetail {

    param (
        [Parameter(Mandatory)]
        $ErrorRecord
    )

    $Message = $ErrorRecord.Exception.Message

    if (
        $ErrorRecord.ErrorDetails -and
        $ErrorRecord.ErrorDetails.Message
    ) {
        $Message += "`n$($ErrorRecord.ErrorDetails.Message)"
    }

    return $Message
}


function Get-HttpStatusCode {

    param (
        [Parameter(Mandatory)]
        $ErrorRecord
    )

    try {
        if ($ErrorRecord.Exception.Response.StatusCode) {
            return [int]$ErrorRecord.Exception.Response.StatusCode
        }
    }
    catch {
    }

    return $null
}


# ============================================================
# Generic REST Helper
# ============================================================

function Invoke-ApiRest {

    param (
        [Parameter(Mandatory)]
        [string]$Uri,

        [Parameter(Mandatory)]
        [ValidateSet(
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE"
        )]
        [string]$Method,

        [System.Collections.IDictionary]$Headers,

        [object]$Body,

        [string]$ContentType = "application/json",

        [bool]$IgnoreCertificateErrors = $false
    )

    $Params = @{
        Uri         = $Uri
        Method      = $Method
        ErrorAction = "Stop"
    }

    if ($null -ne $Headers) {
        $Params.Headers = $Headers
    }

    if ($null -ne $Body) {

        $Params.ContentType = $ContentType

        if ($Body -is [string]) {
            $Params.Body = $Body
        }
        else {
            $Params.Body = $Body |
                ConvertTo-Json -Depth 30
        }
    }

    $Command = Get-Command Invoke-RestMethod

    if (
        $IgnoreCertificateErrors -and
        $Command.Parameters.ContainsKey("SkipCertificateCheck")
    ) {
        $Params.SkipCertificateCheck = $true
    }

    Invoke-RestMethod @Params
}


# ============================================================
# Resolve File Relative To JSON
# ============================================================

function Resolve-ConfigFilePath {

    param (
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$ConfigDirectory
    )

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return $Path
    }

    return Join-Path `
        -Path $ConfigDirectory `
        -ChildPath $Path
}


# ============================================================
# vCenter REST Authentication
# ============================================================

function New-VCenterRestSession {

    param (
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [string]$Username,

        [Parameter(Mandatory)]
        [string]$Password,

        [bool]$IgnoreCertificateErrors = $false
    )

    Write-Info "Creating vCenter REST session"

    $Bytes = [Text.Encoding]::UTF8.GetBytes(
        "${Username}:${Password}"
    )

    $Basic = [Convert]::ToBase64String($Bytes)

    $AuthHeaders =
        [System.Collections.Generic.Dictionary[string,string]]::new()

    $AuthHeaders.Add(
        "Authorization",
        "Basic $Basic"
    )

    $SessionId = Invoke-ApiRest `
        -Uri "https://$Server/api/session" `
        -Method POST `
        -Headers $AuthHeaders `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    if (
        [string]::IsNullOrWhiteSpace(
            [string]$SessionId
        )
    ) {
        throw "vCenter REST authentication failed."
    }

    $Headers =
        [System.Collections.Generic.Dictionary[string,string]]::new()

    $Headers.Add(
        "vmware-api-session-id",
        [string]$SessionId
    )

    return $Headers
}


# ============================================================
# Tag Category
# ============================================================

function Get-ExactTagCategory {

    param (
        [Parameter(Mandatory)]
        [string]$Name
    )

    Get-TagCategory |
        Where-Object {
            $_.Name -eq $Name
        } |
        Select-Object -First 1
}


function Get-OrCreateTagCategory {

    param (
        [Parameter(Mandatory)]
        [string]$Name,

        [string]$Description = "",

        [ValidateSet(
            "Single",
            "Multiple"
        )]
        [string]$Cardinality = "Single",

        [string[]]$EntityTypes
    )

    $Category = Get-ExactTagCategory `
        -Name $Name

    if ($Category) {

        Write-Skip "Category '$Name' already exists"

        return $Category
    }

    Write-Create "Category '$Name'"

    $Parameters = @{
        Name        = $Name
        Description = $Description
        Cardinality = $Cardinality
    }

    if (
        $EntityTypes -and
        $EntityTypes.Count -gt 0
    ) {
        $Parameters.EntityType = $EntityTypes
    }

    return New-TagCategory @Parameters
}


# ============================================================
# Tag
# ============================================================

function Get-ExactTag {

    param (
        [Parameter(Mandatory)]
        [string]$CategoryName,

        [Parameter(Mandatory)]
        [string]$TagName
    )

    Get-Tag |
        Where-Object {
            $_.Name -eq $TagName -and
            $_.Category.Name -eq $CategoryName
        } |
        Select-Object -First 1
}


function Get-OrCreateTag {

    param (
        [Parameter(Mandatory)]
        [string]$Name,

        [Parameter(Mandatory)]
        $Category,

        [string]$Description = ""
    )

    $Tag = Get-ExactTag `
        -CategoryName $Category.Name `
        -TagName $Name

    if ($Tag) {

        Write-Skip (
            "Tag '$($Category.Name)/$Name' already exists"
        )

        return $Tag
    }

    Write-Create (
        "Tag '$($Category.Name)/$Name'"
    )

    return New-Tag `
        -Name $Name `
        -Category $Category `
        -Description $Description
}


# ============================================================
# vSphere Object Lookup
# ============================================================

function Get-vSphereObject {

    param (
        [Parameter(Mandatory)]
        [string]$Type,

        [Parameter(Mandatory)]
        [string]$Name
    )

    switch ($Type.ToLower()) {

        "virtualmachine" {
            return Get-VM `
                -Name $Name `
                -ErrorAction Stop
        }

        "vm" {
            return Get-VM `
                -Name $Name `
                -ErrorAction Stop
        }

        "vmhost" {
            return Get-VMHost `
                -Name $Name `
                -ErrorAction Stop
        }

        "cluster" {
            return Get-Cluster `
                -Name $Name `
                -ErrorAction Stop
        }

        "datacenter" {
            return Get-Datacenter `
                -Name $Name `
                -ErrorAction Stop
        }

        "datastore" {
            return Get-Datastore `
                -Name $Name `
                -ErrorAction Stop
        }

        "datastorecluster" {
            return Get-DatastoreCluster `
                -Name $Name `
                -ErrorAction Stop
        }

        "resourcepool" {
            return Get-ResourcePool `
                -Name $Name `
                -ErrorAction Stop
        }

        "folder" {
            return Get-Folder `
                -Name $Name `
                -ErrorAction Stop
        }

        "vapp" {
            return Get-VApp `
                -Name $Name `
                -ErrorAction Stop
        }

        default {
            throw "Unsupported object type '$Type'"
        }
    }
}


# ============================================================
# Assign Tag Only If Missing
# ============================================================

function Set-TagIfMissing {

    param (
        [Parameter(Mandatory)]
        $Entity,

        [Parameter(Mandatory)]
        $Tag
    )

    $Existing = Get-TagAssignment `
        -Entity $Entity `
        -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Tag.Name -eq $Tag.Name -and
            $_.Tag.Category.Name -eq $Tag.Category.Name
        } |
        Select-Object -First 1

    if ($Existing) {

        Write-Skip (
            "'$($Entity.Name)' already has " +
            "'$($Tag.Category.Name)/$($Tag.Name)'"
        )

        return
    }

    Write-Create (
        "Assign '$($Tag.Category.Name)/$($Tag.Name)' " +
        "to '$($Entity.Name)'"
    )

    New-TagAssignment `
        -Entity $Entity `
        -Tag $Tag |
        Out-Null
}


# ============================================================
# vCenter REST Category ID
# ============================================================

function Get-RestCategoryId {

    param (
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        [string]$CategoryName,

        [bool]$IgnoreCertificateErrors = $false
    )

    $Result = Invoke-ApiRest `
        -Uri "https://$Server/api/vcenter/tagging/categories" `
        -Method GET `
        -Headers $Headers `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    $Category = $Result.items |
        Where-Object {
            $_.info.name -eq $CategoryName
        } |
        Select-Object -First 1

    if (-not $Category) {

        throw (
            "Unable to locate REST category " +
            "'$CategoryName'"
        )
    }

    return $Category.category_id
}


# ============================================================
# vCenter REST Tag ID
# ============================================================

function Get-RestTagId {

    param (
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        [string]$CategoryName,

        [Parameter(Mandatory)]
        [string]$TagName,

        [bool]$IgnoreCertificateErrors = $false
    )

    $CategoryId = Get-RestCategoryId `
        -Server $Server `
        -Headers $Headers `
        -CategoryName $CategoryName `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    $Result = Invoke-ApiRest `
        -Uri "https://$Server/api/vcenter/tagging/tags" `
        -Method GET `
        -Headers $Headers `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    $Tag = $Result.items |
        Where-Object {
            $_.info.name -eq $TagName -and
            $_.info.category -eq $CategoryId
        } |
        Select-Object -First 1

    if (-not $Tag) {

        throw (
            "Unable to locate REST tag " +
            "'$CategoryName/$TagName'"
        )
    }

    return $Tag.tag
}


# ============================================================
# vCenter Compute Policy Lookup
# ============================================================

function Get-RestComputePolicy {

    param (
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        [string]$Name,

        [bool]$IgnoreCertificateErrors = $false
    )

    $Result = Invoke-ApiRest `
        -Uri "https://$Server/api/vcenter/compute/policies" `
        -Method GET `
        -Headers $Headers `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    return $Result |
        Where-Object {
            $_.name -eq $Name
        } |
        Select-Object -First 1
}


# ============================================================
# Compute Policy Capability
# ============================================================

function Get-RestComputePolicyCapability {

    param (
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        [ValidateSet(
            "vm-host-affinity",
            "vm-host-anti-affinity"
        )]
        [string]$Type,

        [bool]$IgnoreCertificateErrors = $false
    )

    $Capabilities = Invoke-ApiRest `
        -Uri (
            "https://$Server/api/vcenter/" +
            "compute/policies/capabilities"
        ) `
        -Method GET `
        -Headers $Headers `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    switch ($Type) {

        "vm-host-affinity" {

            $Capability = $Capabilities |
                Where-Object {
                    $_.capability -match "VmHostAffinity" -or
                    (
                        $_.name -match "host" -and
                        $_.name -match "affinity" -and
                        $_.name -notmatch "anti"
                    )
                } |
                Select-Object -First 1
        }

        "vm-host-anti-affinity" {

            $Capability = $Capabilities |
                Where-Object {
                    $_.capability -match "VmHostAntiAffinity" -or
                    (
                        $_.name -match "host" -and
                        $_.name -match "anti"
                    )
                } |
                Select-Object -First 1
        }
    }

    if (-not $Capability) {

        Write-Warn "Available compute policy capabilities:"

        $Capabilities |
            Select-Object `
                capability,
                name,
                description |
            Format-Table -AutoSize

        throw (
            "Unable to locate compute policy capability '$Type'"
        )
    }

    return $Capability
}


# ============================================================
# Create vCenter Compute Policy If Missing
# ============================================================

function New-RestComputePolicyIfMissing {

    param (
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        $Policy,

        [bool]$IgnoreCertificateErrors = $false
    )

    $Existing = Get-RestComputePolicy `
        -Server $Server `
        -Headers $Headers `
        -Name $Policy.name `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    if ($Existing) {

        Write-Skip (
            "vCenter compute policy " +
            "'$($Policy.name)' already exists"
        )

        return
    }

    $VMTagId = Get-RestTagId `
        -Server $Server `
        -Headers $Headers `
        -CategoryName $Policy.vm_tag.category `
        -TagName $Policy.vm_tag.tag `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    $HostTagId = Get-RestTagId `
        -Server $Server `
        -Headers $Headers `
        -CategoryName $Policy.host_tag.category `
        -TagName $Policy.host_tag.tag `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    $Capability = Get-RestComputePolicyCapability `
        -Server $Server `
        -Headers $Headers `
        -Type $Policy.type `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    Write-Info (
        "Using capability '$($Capability.name)'"
    )

    $Body = [ordered]@{
        capability  = $Capability.capability
        name        = $Policy.name
        description = $Policy.description
        vm_tag      = $VMTagId
        host_tag    = $HostTagId
    }

    if (
        $Policy.PSObject.Properties.Name `
            -contains "strictness"
    ) {
        $Body.strictness = $Policy.strictness
    }

    Write-Create (
        "vCenter compute policy '$($Policy.name)'"
    )

    try {

        $Result = Invoke-ApiRest `
            -Uri (
                "https://$Server/api/vcenter/compute/policies"
            ) `
            -Method POST `
            -Headers $Headers `
            -Body $Body `
            -IgnoreCertificateErrors $IgnoreCertificateErrors

        Write-Host (
            "[CREATED] vCenter compute policy " +
            "'$($Policy.name)' ID=$Result"
        ) -ForegroundColor Green
    }
    catch {

        $Status = Get-HttpStatusCode $_

        Write-Host ""
        Write-Host (
            "vCenter compute policy creation failed"
        ) -ForegroundColor Red

        Write-Host "Policy: $($Policy.name)"
        Write-Host "HTTP:   $Status"

        Write-Host ""
        Write-Host (
            Get-RestErrorDetail $_
        )

        if ($Status -eq 403) {

            Write-Warn (
                "vCenter denied the request. " +
                "Check ComputePolicy.Manage privileges."
            )
        }

        throw
    }
}


# ============================================================
# Read VCFA API / Refresh Token
# ============================================================

function Get-VcfaApiToken {

    param (
        [Parameter(Mandatory)]
        [string]$TokenFile
    )

    if (-not (Test-Path $TokenFile)) {

        throw (
            "VCFA API token file not found: $TokenFile"
        )
    }

    $Raw = (
        Get-Content `
            -Path $TokenFile `
            -Raw
    ).Trim()

    if ([string]::IsNullOrWhiteSpace($Raw)) {

        throw (
            "VCFA API token file is empty: $TokenFile"
        )
    }

    if ($Raw.StartsWith("{")) {

        $Object = $Raw |
            ConvertFrom-Json

        if (
            $Object.PSObject.Properties.Name `
                -contains "refresh_token"
        ) {

            return (
                [string]$Object.refresh_token
            ).Trim()
        }

        if (
            $Object.PSObject.Properties.Name `
                -contains "api_token"
        ) {

            return (
                [string]$Object.api_token
            ).Trim()
        }

        if (
            $Object.PSObject.Properties.Name `
                -contains "token"
        ) {

            return (
                [string]$Object.token
            ).Trim()
        }

        throw (
            "VCFA token JSON must contain " +
            "refresh_token, api_token or token."
        )
    }

    return $Raw
}


# ============================================================
# Exchange VCFA API Token For Access Token
# ============================================================

function Get-VcfaAccessToken {

    param (
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [string]$ApiToken,

        [bool]$IgnoreCertificateErrors = $false
    )

    Write-Info (
        "Exchanging VCFA API token for bearer token"
    )

    $Uri = (
        "https://$Server/oauth/provider/token"
    )

    $EncodedToken = [uri]::EscapeDataString(
        $ApiToken
    )

    $Body = (
        "grant_type=refresh_token" +
        "&refresh_token=$EncodedToken"
    )

    $Headers =
        [System.Collections.Generic.Dictionary[string,string]]::new()

    $Headers.Add(
        "Accept",
        "application/json"
    )

    try {

        $Result = Invoke-ApiRest `
            -Uri $Uri `
            -Method POST `
            -Headers $Headers `
            -Body $Body `
            -ContentType `
                "application/x-www-form-urlencoded" `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors
    }
    catch {

        $Status = Get-HttpStatusCode $_

        Write-Host ""
        Write-Host (
            "VCFA API token exchange failed"
        ) -ForegroundColor Red

        Write-Host "Endpoint: $Uri"
        Write-Host "HTTP:     $Status"

        Write-Host ""
        Write-Host (
            Get-RestErrorDetail $_
        )

        throw
    }

    if (
        -not (
            $Result.PSObject.Properties.Name `
                -contains "access_token"
        )
    ) {

        throw (
            "VCFA token response did not contain access_token."
        )
    }

    if (
        [string]::IsNullOrWhiteSpace(
            [string]$Result.access_token
        )
    ) {

        throw (
            "VCFA returned an empty access_token."
        )
    }

    Write-Info (
        "VCFA bearer token obtained successfully"
    )

    if (
        $Result.PSObject.Properties.Name `
            -contains "expires_in"
    ) {

        Write-Info (
            "VCFA token lifetime: " +
            "$($Result.expires_in) seconds"
        )
    }

    return [string]$Result.access_token
}


# ============================================================
# VCFA Request Headers
# ============================================================

function New-VcfaHeaders {

    param (
        [Parameter(Mandatory)]
        [string]$AccessToken
    )

    $Headers =
        [System.Collections.Generic.Dictionary[string,string]]::new()

    $Headers.Add(
        "Authorization",
        "Bearer $AccessToken"
    )

    $Headers.Add(
        "Accept",
        "application/json;version=9.1.0"
    )

    return $Headers
}


# ============================================================
# Get VCFA Infrastructure Policies
# ============================================================

function Get-VcfaInfrastructurePolicies {

    param (
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [bool]$IgnoreCertificateErrors = $false
    )

    $Policies = @()
    $Page = 1
    $PageSize = 128

    do {

        $Result = Invoke-ApiRest `
            -Uri (
                "https://$Server/cloudapi/v1/infraPolicies" +
                "?page=$Page&pageSize=$PageSize"
            ) `
            -Method GET `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

        if ($Result.values) {
            $Policies += @(
                $Result.values
            )
        }

        $Page++

    } while (
        $Result.pageCount -gt 0 -and
        $Page -le $Result.pageCount
    )

    return $Policies
}


# ============================================================
# Find VCFA Infrastructure Policy
# ============================================================

function Get-VcfaInfrastructurePolicy {

    param (
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        [string]$Name,

        [bool]$IgnoreCertificateErrors = $false
    )

    $Policies = Get-VcfaInfrastructurePolicies `
        -Server $Server `
        -Headers $Headers `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    return $Policies |
        Where-Object {
            $_.name -eq $Name
        } |
        Select-Object -First 1
}


# ============================================================
# Create VCFA Infrastructure Policy If Missing
# ============================================================

function New-VcfaInfrastructurePolicyIfMissing {

    param (
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        $Policy,

        [bool]$IgnoreCertificateErrors = $false
    )

    if (
        $Policy.name.Length -gt 63 -or
        $Policy.name -notmatch `
            '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'
    ) {

        throw (
            "Invalid VCFA infrastructure policy name " +
            "'$($Policy.name)'"
        )
    }

    $Existing = Get-VcfaInfrastructurePolicy `
        -Server $Server `
        -Headers $Headers `
        -Name $Policy.name `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors

    if ($Existing) {

        Write-Skip (
            "VCFA infrastructure policy " +
            "'$($Policy.name)' already exists"
        )

        return
    }

    $Body = [ordered]@{
        name = $Policy.name
    }

    if (
        $Policy.PSObject.Properties.Name `
            -contains "description"
    ) {

        if (
            -not [string]::IsNullOrWhiteSpace(
                [string]$Policy.description
            )
        ) {
            $Body.description =
                $Policy.description
        }
    }

    if (
        $Policy.PSObject.Properties.Name `
            -contains "vc_compute_policy_name"
    ) {

        if (
            -not [string]::IsNullOrWhiteSpace(
                [string]$Policy.vc_compute_policy_name
            )
        ) {
            $Body.vcComputePolicyName =
                $Policy.vc_compute_policy_name
        }
    }

    if (
        $Policy.PSObject.Properties.Name `
            -contains "is_mandatory"
    ) {

        $Body.isMandatory =
            [bool]$Policy.is_mandatory
    }

    if (
        $Policy.PSObject.Properties.Name `
            -contains "policy_rule"
    ) {

        if ($null -ne $Policy.policy_rule) {
            $Body.policyRule =
                $Policy.policy_rule
        }
    }

    if (
        $Policy.PSObject.Properties.Name `
            -contains "compatible_region_zones"
    ) {

        if (
            $null -ne `
                $Policy.compatible_region_zones
        ) {
            $Body.compatibleRegionZones =
                $Policy.compatible_region_zones
        }
    }

    Write-Create (
        "VCFA infrastructure policy " +
        "'$($Policy.name)'"
    )

    try {

        Invoke-ApiRest `
            -Uri (
                "https://$Server/cloudapi/v1/infraPolicies"
            ) `
            -Method POST `
            -Headers $Headers `
            -Body $Body `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors |
            Out-Null

        Write-Host (
            "[ACCEPTED] VCFA infrastructure policy " +
            "'$($Policy.name)'"
        ) -ForegroundColor Green
    }
    catch {

        $Status = Get-HttpStatusCode $_

        Write-Host ""
        Write-Host (
            "VCFA infrastructure policy creation failed"
        ) -ForegroundColor Red

        Write-Host "Policy: $($Policy.name)"
        Write-Host "HTTP:   $Status"

        Write-Host ""
        Write-Host (
            Get-RestErrorDetail $_
        )

        if ($Status -eq 401) {

            Write-Warn (
                "VCFA bearer token was rejected."
            )
        }

        if ($Status -eq 403) {

            Write-Warn (
                "VCFA authenticated the token but denied " +
                "the operation. Check provider rights."
            )
        }

        throw
    }
}


# ============================================================
# Load JSON
# ============================================================

$ResolvedConfig = (
    Resolve-Path $ConfigFile
).Path

$ConfigDirectory = Split-Path `
    -Parent `
    $ResolvedConfig

Write-Info (
    "Loading configuration '$ResolvedConfig'"
)

$Config = Get-Content `
    -Path $ResolvedConfig `
    -Raw |
    ConvertFrom-Json


# ============================================================
# Mandatory vCenter Section
# ============================================================

if (
    -not (
        $Config.PSObject.Properties.Name `
            -contains "vcenter"
    )
) {
    throw "vcenter section is required"
}

if (-not $Config.vcenter.server) {
    throw "vcenter.server is required"
}

if (-not $Config.vcenter.username) {
    throw "vcenter.username is required"
}

if (-not $Config.vcenter.password_file) {
    throw "vcenter.password_file is required"
}


# ============================================================
# vCenter Password
# ============================================================

$PasswordFile = Resolve-ConfigFilePath `
    -Path $Config.vcenter.password_file `
    -ConfigDirectory $ConfigDirectory

if (-not (Test-Path $PasswordFile)) {

    throw (
        "vCenter password file not found: $PasswordFile"
    )
}

$Password = (
    Get-Content `
        -Path $PasswordFile `
        -Raw
).Trim()

if (
    [string]::IsNullOrWhiteSpace(
        $Password
    )
) {

    throw "vCenter password file is empty"
}

$SecurePassword = ConvertTo-SecureString `
    -String $Password `
    -AsPlainText `
    -Force

$Credential = [PSCredential]::new(
    $Config.vcenter.username,
    $SecurePassword
)


# ============================================================
# Certificate Configuration
# ============================================================

$IgnoreCertErrors = $false

if (
    $Config.vcenter.PSObject.Properties.Name `
        -contains "ignore_certificate_errors"
) {

    $IgnoreCertErrors =
        [bool]$Config.vcenter.ignore_certificate_errors
}

if ($IgnoreCertErrors) {

    Set-PowerCLIConfiguration `
        -InvalidCertificateAction Ignore `
        -Confirm:$false |
        Out-Null
}


# ============================================================
# Determine Whether VCFA Is Configured
# ============================================================

$VcfaConfigured = $false

if (
    ($Config.PSObject.Properties.Name -contains "vcfa") -and
    ($null -ne $Config.vcfa) -and
    -not [string]::IsNullOrWhiteSpace(
        [string]$Config.vcfa.server
    )
) {
    $VcfaConfigured = $true
}


# ============================================================
# VCFA Authentication
# ============================================================

$VcfaHeaders = $null
$VcfaIgnoreCertErrors = $false

if ($VcfaConfigured) {

    if (
        [string]::IsNullOrWhiteSpace(
            [string]$Config.vcfa.api_token_file
        )
    ) {

        throw (
            "vcfa.api_token_file is required " +
            "when VCFA is configured."
        )
    }

    if (
        $Config.vcfa.PSObject.Properties.Name `
            -contains "ignore_certificate_errors"
    ) {

        $VcfaIgnoreCertErrors =
            [bool]$Config.vcfa.ignore_certificate_errors
    }

    $VcfaTokenFile = Resolve-ConfigFilePath `
        -Path $Config.vcfa.api_token_file `
        -ConfigDirectory $ConfigDirectory

    $VcfaApiToken = Get-VcfaApiToken `
        -TokenFile $VcfaTokenFile

    Write-Info (
        "VCFA API token loaded from '$VcfaTokenFile'"
    )

    $VcfaAccessToken = Get-VcfaAccessToken `
        -Server $Config.vcfa.server `
        -ApiToken $VcfaApiToken `
        -IgnoreCertificateErrors `
            $VcfaIgnoreCertErrors

    $VcfaHeaders = New-VcfaHeaders `
        -AccessToken $VcfaAccessToken

    $VcfaApiToken = $null

    Write-Info (
        "VCFA authenticated to '$($Config.vcfa.server)'"
    )
}
else {

    Write-Skip "VCFA configuration not provided"
}


# ============================================================
# Main
# ============================================================

$VIServer = $null
$RestHeaders = $null

try {

    # ========================================================
    # vCenter Connection
    # ========================================================

    Write-Info (
        "Connecting PowerCLI to " +
        "'$($Config.vcenter.server)'"
    )

    $VIServer = Connect-VIServer `
        -Server $Config.vcenter.server `
        -Credential $Credential `
        -ErrorAction Stop

    $RestHeaders = New-VCenterRestSession `
        -Server $Config.vcenter.server `
        -Username $Config.vcenter.username `
        -Password $Password `
        -IgnoreCertificateErrors `
            $IgnoreCertErrors


    # ========================================================
    # Categories / Tags - OPTIONAL
    # ========================================================

    if (
        ($Config.PSObject.Properties.Name -contains "categories") -and
        ($null -ne $Config.categories) -and
        (@($Config.categories).Count -gt 0)
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " Categories and Tags"
        Write-Host "========================================"

        foreach (
            $CategoryConfig in @($Config.categories)
        ) {

            $Description = ""

            if (
                $CategoryConfig.PSObject.Properties.Name `
                    -contains "description"
            ) {

                if ($null -ne $CategoryConfig.description) {

                    $Description =
                        [string]$CategoryConfig.description
                }
            }

            $EntityTypes = @()

            if (
                $CategoryConfig.PSObject.Properties.Name `
                    -contains "entity_types"
            ) {

                $EntityTypes = @(
                    $CategoryConfig.entity_types
                )
            }

            $Cardinality = "Single"

            if (
                $CategoryConfig.PSObject.Properties.Name `
                    -contains "cardinality"
            ) {

                if (
                    -not [string]::IsNullOrWhiteSpace(
                        [string]$CategoryConfig.cardinality
                    )
                ) {
                    $Cardinality =
                        [string]$CategoryConfig.cardinality
                }
            }

            $Category = Get-OrCreateTagCategory `
                -Name $CategoryConfig.name `
                -Description $Description `
                -Cardinality $Cardinality `
                -EntityTypes $EntityTypes


            if (
                ($CategoryConfig.PSObject.Properties.Name -contains "tags") -and
                ($null -ne $CategoryConfig.tags) -and
                (@($CategoryConfig.tags).Count -gt 0)
            ) {

                foreach (
                    $TagConfig in @($CategoryConfig.tags)
                ) {

                    $TagDescription = ""

                    if (
                        $TagConfig.PSObject.Properties.Name `
                            -contains "description"
                    ) {

                        if ($null -ne $TagConfig.description) {

                            $TagDescription =
                                [string]$TagConfig.description
                        }
                    }

                    Get-OrCreateTag `
                        -Name $TagConfig.name `
                        -Category $Category `
                        -Description $TagDescription |
                        Out-Null
                }
            }
            else {

                Write-Skip (
                    "No tags configured for category " +
                    "'$($CategoryConfig.name)'"
                )
            }
        }
    }
    else {

        Write-Skip "No vCenter categories configured"
    }


    # ========================================================
    # Tag Assignments - OPTIONAL
    # ========================================================

    if (
        ($Config.PSObject.Properties.Name -contains "assignments") -and
        ($null -ne $Config.assignments) -and
        (@($Config.assignments).Count -gt 0)
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " Tag Assignments"
        Write-Host "========================================"

        foreach (
            $Assignment in @($Config.assignments)
        ) {

            $Entity = Get-vSphereObject `
                -Type $Assignment.type `
                -Name $Assignment.name


            if (
                ($Assignment.PSObject.Properties.Name -contains "tags") -and
                ($null -ne $Assignment.tags) -and
                (@($Assignment.tags).Count -gt 0)
            ) {

                foreach (
                    $TagConfig in @($Assignment.tags)
                ) {

                    $Tag = Get-ExactTag `
                        -CategoryName $TagConfig.category `
                        -TagName $TagConfig.tag

                    if (-not $Tag) {

                        throw (
                            "Tag '$($TagConfig.category)/" +
                            "$($TagConfig.tag)' referenced by " +
                            "'$($Assignment.name)' does not exist."
                        )
                    }

                    Set-TagIfMissing `
                        -Entity $Entity `
                        -Tag $Tag
                }
            }
            else {

                Write-Skip (
                    "No tags configured for object " +
                    "'$($Assignment.name)'"
                )
            }
        }
    }
    else {

        Write-Skip "No vCenter tag assignments configured"
    }


    # ========================================================
    # vCenter Compute Policies - OPTIONAL
    # ========================================================

    if (
        ($Config.PSObject.Properties.Name -contains "policies") -and
        ($null -ne $Config.policies) -and
        (@($Config.policies).Count -gt 0)
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " vCenter Compute Policies"
        Write-Host "========================================"

        foreach (
            $Policy in @($Config.policies)
        ) {

            New-RestComputePolicyIfMissing `
                -Server $Config.vcenter.server `
                -Headers $RestHeaders `
                -Policy $Policy `
                -IgnoreCertificateErrors `
                    $IgnoreCertErrors
        }
    }
    else {

        Write-Skip "No vCenter compute policies configured"
    }


    # ========================================================
    # VCFA Infrastructure Policies - OPTIONAL
    # ========================================================

    if (
        $VcfaConfigured -and
        ($Config.PSObject.Properties.Name -contains "infrastructure_policies") -and
        ($null -ne $Config.infrastructure_policies) -and
        (@($Config.infrastructure_policies).Count -gt 0)
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " VCFA Infrastructure Policies"
        Write-Host "========================================"

        foreach (
            $Policy in @(
                $Config.infrastructure_policies
            )
        ) {

            if (
                -not (
                    $Policy.PSObject.Properties.Name `
                        -contains "vc_compute_policy_name"
                ) -or
                [string]::IsNullOrWhiteSpace(
                    [string]$Policy.vc_compute_policy_name
                )
            ) {

                throw (
                    "VCFA infrastructure policy " +
                    "'$($Policy.name)' must define " +
                    "vc_compute_policy_name."
                )
            }

            $VCPolicy = Get-RestComputePolicy `
                -Server $Config.vcenter.server `
                -Headers $RestHeaders `
                -Name $Policy.vc_compute_policy_name `
                -IgnoreCertificateErrors `
                    $IgnoreCertErrors

            if (-not $VCPolicy) {

                throw (
                    "Referenced vCenter compute policy " +
                    "'$($Policy.vc_compute_policy_name)' " +
                    "does not exist."
                )
            }

            Write-Info (
                "Verified vCenter compute policy " +
                "'$($Policy.vc_compute_policy_name)'"
            )

            New-VcfaInfrastructurePolicyIfMissing `
                -Server $Config.vcfa.server `
                -Headers $VcfaHeaders `
                -Policy $Policy `
                -IgnoreCertificateErrors `
                    $VcfaIgnoreCertErrors
        }
    }
    elseif ($VcfaConfigured) {

        Write-Skip (
            "No VCFA infrastructure policies defined"
        )
    }


    # ========================================================
    # vCenter Compute Policy Summary - ONLY IF POLICIES CONFIGURED
    # ========================================================

    if (
        ($Config.PSObject.Properties.Name -contains "policies") -and
        ($null -ne $Config.policies) -and
        (@($Config.policies).Count -gt 0)
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " vCenter Compute Policy Summary"
        Write-Host "========================================"

        $VCPolicies = Invoke-ApiRest `
            -Uri (
                "https://$($Config.vcenter.server)" +
                "/api/vcenter/compute/policies"
            ) `
            -Method GET `
            -Headers $RestHeaders `
            -IgnoreCertificateErrors `
                $IgnoreCertErrors

        $VCPolicies |
            Select-Object `
                name,
                policy,
                capability,
                description |
            Format-Table -AutoSize
    }


    # ========================================================
    # VCFA Summary
    # ========================================================

    if (
        $VcfaConfigured -and
        ($Config.PSObject.Properties.Name -contains "infrastructure_policies") -and
        ($null -ne $Config.infrastructure_policies) -and
        (@($Config.infrastructure_policies).Count -gt 0)
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " VCFA Infrastructure Policy Summary"
        Write-Host "========================================"

        $InfraPolicies =
            Get-VcfaInfrastructurePolicies `
                -Server $Config.vcfa.server `
                -Headers $VcfaHeaders `
                -IgnoreCertificateErrors `
                    $VcfaIgnoreCertErrors

        $InfraPolicies |
            Select-Object `
                name,
                vcComputePolicyName,
                isMandatory,
                creationStatus,
                syncedToVCenters |
            Format-Table -AutoSize
    }


    Write-Host ""
    Write-Host (
        "Configuration completed successfully."
    ) -ForegroundColor Green
}
finally {

    # ========================================================
    # Close vCenter REST Session
    # ========================================================

    if ($RestHeaders) {

        try {

            Invoke-ApiRest `
                -Uri (
                    "https://$($Config.vcenter.server)" +
                    "/api/session"
                ) `
                -Method DELETE `
                -Headers $RestHeaders `
                -IgnoreCertificateErrors `
                    $IgnoreCertErrors |
                Out-Null
        }
        catch {

            Write-Warn (
                "Unable to close vCenter REST session"
            )
        }
    }


    # ========================================================
    # Disconnect PowerCLI
    # ========================================================

    if ($VIServer) {

        Disconnect-VIServer `
            -Server $VIServer `
            -Confirm:$false
    }
}