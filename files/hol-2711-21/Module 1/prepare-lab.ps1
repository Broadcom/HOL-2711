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
# Generic property helper
# ============================================================

function Test-Property {

    param (
        [object]$Object,
        [string]$Name
    )

    return (
        $null -ne $Object -and
        $Object.PSObject.Properties.Name -contains $Name
    )
}


# ============================================================
# REST error helpers
# ============================================================

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


# ============================================================
# Generic REST helper
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
                ConvertTo-Json -Depth 50
        }
    }

    $IRM = Get-Command Invoke-RestMethod

    if (
        $IgnoreCertificateErrors -and
        $IRM.Parameters.ContainsKey(
            "SkipCertificateCheck"
        )
    ) {
        $Params.SkipCertificateCheck = $true
    }

    Invoke-RestMethod @Params
}


# ============================================================
# Resolve file relative to config
# ============================================================

function Resolve-ConfigFilePath {

    param (
        [string]$Path,
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
# Generic VCFA paged query
# ============================================================

function Get-VcfaPagedValues {

    param (
        [string]$Server,
        [string]$Path,
        [System.Collections.IDictionary]$Headers,
        [int]$PageSize = 128,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Values = @()
    $Page = 1

    do {

        $Separator = "?"

        if ($Path.Contains("?")) {
            $Separator = "&"
        }

        $Uri = (
            "https://$Server$Path" +
            "${Separator}page=$Page&pageSize=$PageSize"
        )

        $Result = Invoke-ApiRest `
            -Uri $Uri `
            -Method GET `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

        if (
            $Result -and
            (Test-Property $Result "values") -and
            $Result.values
        ) {
            $Values += @($Result.values)
        }

        $PageCount = 1

        if (
            $Result -and
            (Test-Property $Result "pageCount") -and
            $Result.pageCount
        ) {
            $PageCount = [int]$Result.pageCount
        }

        $Page++

    } while ($Page -le $PageCount)

    return $Values
}


# ============================================================
# vCenter REST authentication
# ============================================================

function New-VCenterRestSession {

    param (
        [string]$Server,
        [string]$Username,
        [string]$Password,
        [bool]$IgnoreCertificateErrors = $false
    )

    Write-Info "Creating vCenter REST session"

    $Bytes = [Text.Encoding]::UTF8.GetBytes(
        "${Username}:${Password}"
    )

    $Basic = [Convert]::ToBase64String($Bytes)

    $AuthHeaders =
        [System.Collections.Generic.Dictionary[
            string,
            string
        ]]::new()

    $AuthHeaders.Add(
        "Authorization",
        "Basic $Basic"
    )

    $SessionId = Invoke-ApiRest `
        -Uri "https://$Server/api/session" `
        -Method POST `
        -Headers $AuthHeaders `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors

    if (
        [string]::IsNullOrWhiteSpace(
            [string]$SessionId
        )
    ) {
        throw "vCenter REST authentication failed."
    }

    $Headers =
        [System.Collections.Generic.Dictionary[
            string,
            string
        ]]::new()

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
        [string]$Name
    )

    return Get-TagCategory |
        Where-Object {
            $_.Name -eq $Name
        } |
        Select-Object -First 1
}


function Get-OrCreateTagCategory {

    param (
        [string]$Name,
        [string]$Description = "",

        [ValidateSet(
            "Single",
            "Multiple"
        )]
        [string]$Cardinality = "Single",

        [string[]]$EntityTypes
    )

    $Category =
        Get-ExactTagCategory `
            -Name $Name

    if ($Category) {

        Write-Skip (
            "Category '$Name' already exists"
        )

        return $Category
    }

    Write-Create "Category '$Name'"

    $Params = @{
        Name        = $Name
        Description = $Description
        Cardinality = $Cardinality
    }

    if (
        $EntityTypes -and
        $EntityTypes.Count -gt 0
    ) {
        $Params.EntityType =
            $EntityTypes
    }

    return New-TagCategory @Params
}


# ============================================================
# Tags
# ============================================================

function Get-ExactTag {

    param (
        [string]$CategoryName,
        [string]$TagName
    )

    return Get-Tag |
        Where-Object {
            $_.Name -eq $TagName -and
            $_.Category.Name -eq $CategoryName
        } |
        Select-Object -First 1
}


function Get-OrCreateTag {

    param (
        [string]$Name,
        $Category,
        [string]$Description = ""
    )

    $Tag =
        Get-ExactTag `
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
# vSphere object lookup
# ============================================================

function Get-vSphereObject {

    param (
        [string]$Type,
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
            throw (
                "Unsupported object type '$Type'"
            )
        }
    }
}


# ============================================================
# Tag assignment
# ============================================================

function Set-TagIfMissing {

    param (
        $Entity,
        $Tag
    )

    $Existing =
        Get-TagAssignment `
            -Entity $Entity `
            -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Tag.Name -eq $Tag.Name -and
            $_.Tag.Category.Name -eq
                $Tag.Category.Name
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
        "Assign '$($Tag.Category.Name)/" +
        "$($Tag.Name)' to '$($Entity.Name)'"
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
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [string]$CategoryName,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Result =
        Invoke-ApiRest `
            -Uri (
                "https://$Server/api/vcenter/" +
                "tagging/categories"
            ) `
            -Method GET `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    $Category =
        $Result.items |
        Where-Object {
            $_.info.name -eq $CategoryName
        } |
        Select-Object -First 1

    if (-not $Category) {

        throw (
            "REST category '$CategoryName' not found."
        )
    }

    return $Category.category_id
}


# ============================================================
# vCenter REST Tag ID
# ============================================================

function Get-RestTagId {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [string]$CategoryName,
        [string]$TagName,
        [bool]$IgnoreCertificateErrors = $false
    )

    $CategoryId =
        Get-RestCategoryId `
            -Server $Server `
            -Headers $Headers `
            -CategoryName $CategoryName `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    $Result =
        Invoke-ApiRest `
            -Uri (
                "https://$Server/api/vcenter/" +
                "tagging/tags"
            ) `
            -Method GET `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    $Tag =
        $Result.items |
        Where-Object {
            $_.info.name -eq $TagName -and
            $_.info.category -eq $CategoryId
        } |
        Select-Object -First 1

    if (-not $Tag) {

        throw (
            "REST tag '$CategoryName/$TagName' not found."
        )
    }

    return $Tag.tag
}


# ============================================================
# vCenter Compute Policies
# ============================================================

function Get-RestComputePolicy {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [string]$Name,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Policies =
        Invoke-ApiRest `
            -Uri (
                "https://$Server/api/vcenter/" +
                "compute/policies"
            ) `
            -Method GET `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    return $Policies |
        Where-Object {
            $_.name -eq $Name
        } |
        Select-Object -First 1
}


function Get-RestComputePolicyCapability {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,

        [ValidateSet(
            "vm-host-affinity",
            "vm-host-anti-affinity"
        )]
        [string]$Type,

        [bool]$IgnoreCertificateErrors = $false
    )

    $Capabilities =
        Invoke-ApiRest `
            -Uri (
                "https://$Server/api/vcenter/" +
                "compute/policies/capabilities"
            ) `
            -Method GET `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    switch ($Type) {

        "vm-host-affinity" {

            $Capability =
                $Capabilities |
                Where-Object {
                    $_.capability -match
                        "VmHostAffinity" -or
                    (
                        $_.name -match "host" -and
                        $_.name -match "affinity" -and
                        $_.name -notmatch "anti"
                    )
                } |
                Select-Object -First 1
        }

        "vm-host-anti-affinity" {

            $Capability =
                $Capabilities |
                Where-Object {
                    $_.capability -match
                        "VmHostAntiAffinity" -or
                    (
                        $_.name -match "host" -and
                        $_.name -match "anti"
                    )
                } |
                Select-Object -First 1
        }
    }

    if (-not $Capability) {

        throw (
            "Compute policy capability '$Type' not found."
        )
    }

    return $Capability
}


function New-RestComputePolicyIfMissing {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        $Policy,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Existing =
        Get-RestComputePolicy `
            -Server $Server `
            -Headers $Headers `
            -Name $Policy.name `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    if ($Existing) {

        Write-Skip (
            "vCenter compute policy " +
            "'$($Policy.name)' already exists"
        )

        return
    }

    $VMTagId =
        Get-RestTagId `
            -Server $Server `
            -Headers $Headers `
            -CategoryName $Policy.vm_tag.category `
            -TagName $Policy.vm_tag.tag `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    $HostTagId =
        Get-RestTagId `
            -Server $Server `
            -Headers $Headers `
            -CategoryName $Policy.host_tag.category `
            -TagName $Policy.host_tag.tag `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    $Capability =
        Get-RestComputePolicyCapability `
            -Server $Server `
            -Headers $Headers `
            -Type $Policy.type `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    $Body = [ordered]@{
        capability  = $Capability.capability
        name        = $Policy.name
        description = $Policy.description
        vm_tag      = $VMTagId
        host_tag    = $HostTagId
    }

    Write-Create (
        "vCenter compute policy '$($Policy.name)'"
    )

    Invoke-ApiRest `
        -Uri (
            "https://$Server/api/vcenter/" +
            "compute/policies"
        ) `
        -Method POST `
        -Headers $Headers `
        -Body $Body `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors |
        Out-Null
}


# ============================================================
# VCFA authentication
# ============================================================

function Get-VcfaApiToken {

    param (
        [string]$TokenFile
    )

    if (-not (Test-Path $TokenFile)) {

        throw (
            "VCFA API token file not found: " +
            "$TokenFile"
        )
    }

    $Raw = (
        Get-Content `
            -Path $TokenFile `
            -Raw
    ).Trim()

    if ($Raw.StartsWith("{")) {

        $Object =
            $Raw |
            ConvertFrom-Json

        foreach (
            $Property in @(
                "refresh_token",
                "api_token",
                "token"
            )
        ) {

            if (
                $Object.PSObject.Properties.Name `
                    -contains $Property
            ) {

                return (
                    [string]$Object.$Property
                ).Trim()
            }
        }

        throw (
            "VCFA token JSON contains no supported token."
        )
    }

    return $Raw
}


function Get-VcfaAccessToken {

    param (
        [string]$Server,
        [string]$ApiToken,
        [bool]$IgnoreCertificateErrors = $false
    )

    Write-Info (
        "Exchanging VCFA API token for bearer token"
    )

    $Body = (
        "grant_type=refresh_token&refresh_token=" +
        [uri]::EscapeDataString($ApiToken)
    )

    $Headers =
        [System.Collections.Generic.Dictionary[
            string,
            string
        ]]::new()

    $Headers.Add(
        "Accept",
        "application/json"
    )

    $Result =
        Invoke-ApiRest `
            -Uri (
                "https://$Server/oauth/provider/token"
            ) `
            -Method POST `
            -Headers $Headers `
            -Body $Body `
            -ContentType `
                "application/x-www-form-urlencoded" `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    if (-not $Result.access_token) {

        throw (
            "VCFA access token was not returned."
        )
    }

    return [string]$Result.access_token
}


function New-VcfaHeaders {

    param (
        [string]$AccessToken
    )

    $Headers =
        [System.Collections.Generic.Dictionary[
            string,
            string
        ]]::new()

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


function New-VcfaTenantHeaders {

    param (
        [System.Collections.IDictionary]$Headers,
        [string]$OrgId
    )

    $Result =
        [System.Collections.Generic.Dictionary[
            string,
            string
        ]]::new()

    foreach ($Key in $Headers.Keys) {

        $Result.Add(
            [string]$Key,
            [string]$Headers[$Key]
        )
    }

    $Result[
        "X-VMWARE-VCLOUD-TENANT-CONTEXT"
    ] = $OrgId

    return $Result
}


# ============================================================
# VCFA Infrastructure Policies
# ============================================================

function Get-VcfaInfrastructurePolicies {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [bool]$IgnoreCertificateErrors = $false
    )

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/v1/infraPolicies" `
        -Headers $Headers `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors
}


function New-VcfaInfrastructurePolicyIfMissing {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        $Policy,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Existing =
        Get-VcfaInfrastructurePolicies `
            -Server $Server `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors |
        Where-Object {
            $_.name -eq $Policy.name
        } |
        Select-Object -First 1

    if ($Existing) {

        Write-Skip (
            "VCFA infrastructure policy " +
            "'$($Policy.name)' already exists"
        )

        return $Existing
    }

    $Body = [ordered]@{
        name = $Policy.name
    }

    if (
        Test-Property $Policy "description"
    ) {

        $Body.description =
            $Policy.description
    }

    if (
        Test-Property `
            $Policy `
            "vc_compute_policy_name"
    ) {

        $Body.vcComputePolicyName =
            $Policy.vc_compute_policy_name
    }

    if (
        Test-Property `
            $Policy `
            "is_mandatory"
    ) {

        $Body.isMandatory =
            [bool]$Policy.is_mandatory
    }

    Write-Create (
        "VCFA infrastructure policy '$($Policy.name)'"
    )

    Invoke-ApiRest `
        -Uri (
            "https://$Server/cloudapi/v1/" +
            "infraPolicies"
        ) `
        -Method POST `
        -Headers $Headers `
        -Body $Body `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors |
        Out-Null
}


# ============================================================
# VCFA Tenants
# ============================================================

function Get-VcfaTenants {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [bool]$IgnoreCertificateErrors = $false
    )

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/1.0.0/orgs" `
        -Headers $Headers `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors
}


function Get-VcfaTenant {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [string]$Name,
        [bool]$IgnoreCertificateErrors = $false
    )

    return Get-VcfaTenants `
        -Server $Server `
        -Headers $Headers `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors |
        Where-Object {
            $_.name -eq $Name
        } |
        Select-Object -First 1
}


function Wait-VcfaTenantReady {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [string]$Name,
        [bool]$IgnoreCertificateErrors = $false
    )

    for ($i = 0; $i -lt 60; $i++) {

        $Tenant =
            Get-VcfaTenant `
                -Server $Server `
                -Headers $Headers `
                -Name $Name `
                -IgnoreCertificateErrors `
                    $IgnoreCertificateErrors

        if ($Tenant) {

            if (
                -not (
                    Test-Property `
                        $Tenant `
                        "creationStatus"
                ) -or
                $Tenant.creationStatus -eq "READY"
            ) {
                return $Tenant
            }

            if (
                $Tenant.creationStatus -in @(
                    "ERROR",
                    "FAILED_CREATION",
                    "CONFLICT"
                )
            ) {

                throw (
                    "Tenant '$Name' creation failed: " +
                    "$($Tenant.creationStatus)"
                )
            }
        }

        Start-Sleep -Seconds 2
    }

    throw (
        "Timed out waiting for tenant '$Name'."
    )
}


function Get-OrCreateVcfaTenant {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        $Tenant,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Existing =
        Get-VcfaTenant `
            -Server $Server `
            -Headers $Headers `
            -Name $Tenant.name `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    if ($Existing) {

        Write-Skip (
            "VCFA tenant '$($Tenant.name)' already exists"
        )

        return $Existing
    }

    $DisplayName =
        $Tenant.name

    if (
        Test-Property `
            $Tenant `
            "display_name"
    ) {

        $DisplayName =
            $Tenant.display_name
    }

    $Body = [ordered]@{
        name        = $Tenant.name
        displayName = $DisplayName
    }

    if (
        Test-Property `
            $Tenant `
            "description"
    ) {

        $Body.description =
            $Tenant.description
    }

    if (
        Test-Property `
            $Tenant `
            "enabled"
    ) {

        $Body.isEnabled =
            [bool]$Tenant.enabled
    }

    if (
        Test-Property `
            $Tenant `
            "isClassicTenant"
    ) {

        $Body.isClassicTenant =
            [bool]$Tenant.isClassicTenant
    }

    if (
        Test-Property `
            $Tenant `
            "isProviderConsumptionOrg"
    ) {

        $Body.isProviderConsumptionOrg =
            [bool]$Tenant.isProviderConsumptionOrg
    }

    Write-Create (
        "VCFA tenant '$($Tenant.name)'"
    )

    Invoke-ApiRest `
        -Uri (
            "https://$Server/cloudapi/1.0.0/orgs"
        ) `
        -Method POST `
        -Headers $Headers `
        -Body $Body `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors |
        Out-Null

    return Wait-VcfaTenantReady `
        -Server $Server `
        -Headers $Headers `
        -Name $Tenant.name `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors
}


# ============================================================
# Regions
# ============================================================

function Get-VcfaRegions {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [bool]$IgnoreCertificateErrors = $false
    )

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/v1/regions" `
        -Headers $Headers `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors
}


function Get-VcfaRegion {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [string]$Name,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Region =
        Get-VcfaRegions `
            -Server $Server `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors |
        Where-Object {
            $_.name -eq $Name
        } |
        Select-Object -First 1

    if (-not $Region) {

        throw (
            "VCFA region '$Name' not found."
        )
    }

    return $Region
}


# ============================================================
# Zones
# ============================================================

function Get-VcfaZones {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [string]$RegionId,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Zones =
        Get-VcfaPagedValues `
            -Server $Server `
            -Path "/cloudapi/v1/zones" `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    return $Zones |
        Where-Object {

            (
                $_.region -and
                $_.region.id -eq $RegionId
            ) -or
            (
                $_.regionRef -and
                $_.regionRef.id -eq $RegionId
            )
        }
}


# ============================================================
# Supervisors
# ============================================================

function Get-VcfaSupervisors {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [string]$RegionId,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Supervisors =
        Get-VcfaPagedValues `
            -Server $Server `
            -Path "/cloudapi/v1/supervisors" `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    return $Supervisors |
        Where-Object {

            (
                $_.region -and
                $_.region.id -eq $RegionId
            ) -or
            (
                $_.regionRef -and
                $_.regionRef.id -eq $RegionId
            )
        }
}


# ============================================================
# Virtual Datacenters / Region Quotas
# ============================================================

function Get-VcfaVirtualDatacenters {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [bool]$IgnoreCertificateErrors = $false
    )

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/v1/virtualDatacenters" `
        -Headers $Headers `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors
}


function Get-VcfaRegionQuota {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [string]$OrgId,
        [string]$RegionId,
        [bool]$IgnoreCertificateErrors = $false
    )

    return Get-VcfaVirtualDatacenters `
        -Server $Server `
        -Headers $Headers `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors |
        Where-Object {

            (
                (
                    $_.org.id -eq $OrgId -or
                    $_.orgRef.id -eq $OrgId
                ) -and
                (
                    $_.region.id -eq $RegionId -or
                    $_.regionRef.id -eq $RegionId
                )
            )
        } |
        Select-Object -First 1
}


function New-VcfaRegionQuotaIfMissing {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        $Tenant,
        $QuotaConfig,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Region =
        Get-VcfaRegion `
            -Server $Server `
            -Headers $Headers `
            -Name $QuotaConfig.region `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    $Existing =
        Get-VcfaRegionQuota `
            -Server $Server `
            -Headers $Headers `
            -OrgId $Tenant.id `
            -RegionId $Region.id `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    if ($Existing) {

        Write-Skip (
            "Regional quota for '$($Tenant.name)' / " +
            "'$($Region.name)' already exists"
        )

        return $Existing
    }

    $FullAllocation = $false

    if (
        Test-Property `
            $QuotaConfig `
            "share_all"
    ) {

        $FullAllocation =
            [bool]$QuotaConfig.share_all
    }

    $Body = [ordered]@{

        name = (
            "$($Tenant.name)-$($Region.name)"
        )

        org = @{
            name = $Tenant.name
            id   = $Tenant.id
        }

        region = @{
            name = $Region.name
            id   = $Region.id
        }

        isFullAllocation = $FullAllocation
    }


    # --------------------------------------------------------
    # Supervisors
    # --------------------------------------------------------

    if (-not $FullAllocation) {

        $AvailableSupervisors = @(
            Get-VcfaSupervisors `
                -Server $Server `
                -Headers $Headers `
                -RegionId $Region.id `
                -IgnoreCertificateErrors `
                    $IgnoreCertificateErrors
        )

        $SelectedSupervisors = @()

        if (
            (
                Test-Property `
                    $QuotaConfig `
                    "all_supervisors"
            ) -and
            [bool]$QuotaConfig.all_supervisors
        ) {

            $SelectedSupervisors =
                @($AvailableSupervisors)
        }
        elseif (
            Test-Property `
                $QuotaConfig `
                "supervisors"
        ) {

            foreach (
                $SupervisorName in
                @($QuotaConfig.supervisors)
            ) {

                $Supervisor =
                    $AvailableSupervisors |
                    Where-Object {
                        $_.name -eq $SupervisorName
                    } |
                    Select-Object -First 1

                if (-not $Supervisor) {

                    throw (
                        "Supervisor '$SupervisorName' " +
                        "not found in region " +
                        "'$($Region.name)'."
                    )
                }

                $SelectedSupervisors +=
                    $Supervisor
            }
        }

        $Body.supervisors = @(
            $SelectedSupervisors |
            ForEach-Object {

                $SupervisorId = $_.id

                if (
                    -not $SupervisorId -and
                    $_.supervisorId
                ) {
                    $SupervisorId =
                        $_.supervisorId
                }

                @{
                    name = $_.name
                    id   = $SupervisorId
                }
            }
        )
    }


    # --------------------------------------------------------
    # Zone Capacity
    # --------------------------------------------------------

    if (
        -not $FullAllocation -and
        (
            Test-Property `
                $QuotaConfig `
                "capacity"
        )
    ) {

        $Capacity =
            $QuotaConfig.capacity

        $AvailableZones = @(
            Get-VcfaZones `
                -Server $Server `
                -Headers $Headers `
                -RegionId $Region.id `
                -IgnoreCertificateErrors `
                    $IgnoreCertificateErrors
        )

        $ZoneConfigs = @()

        if (
            (
                Test-Property `
                    $Capacity `
                    "all_zones"
            ) -and
            [bool]$Capacity.all_zones
        ) {

            foreach ($Zone in $AvailableZones) {

                $ZoneConfigs +=
                    [PSCustomObject]@{
                        zone =
                            $Zone.name

                        cpu_limit_GHz =
                            0

                        memory_limit_GB =
                            0

                        cpu_reservation_GHz =
                            0

                        memory_reservation_GB =
                            0
                    }
            }
        }
        else {

            $ZoneConfigs =
                @($Capacity.zones)
        }

        $Body.zoneResourceAllocation =
            @()

        foreach ($ZoneConfig in $ZoneConfigs) {

            $Zone =
                $AvailableZones |
                Where-Object {
                    $_.name -eq $ZoneConfig.zone -or
                    $_.zone.name -eq $ZoneConfig.zone
                } |
                Select-Object -First 1

            if (-not $Zone) {

                throw (
                    "Zone '$($ZoneConfig.zone)' not found " +
                    "in region '$($Region.name)'."
                )
            }

            $ZoneName =
                $Zone.name

            $ZoneId =
                $Zone.id

            if (
                Test-Property `
                    $Zone `
                    "zone"
            ) {

                if ($Zone.zone.name) {
                    $ZoneName =
                        $Zone.zone.name
                }

                if ($Zone.zone.id) {
                    $ZoneId =
                        $Zone.zone.id
                }
            }

            $Body.zoneResourceAllocation +=
                [ordered]@{

                    zone = @{
                        name = $ZoneName
                        id   = $ZoneId
                    }

                    resourceAllocation = @{

                        #
                        # GHz -> MHz
                        #

                        cpuLimitMHz =
                            [int64](
                                [double]$ZoneConfig.cpu_limit_GHz *
                                1000
                            )

                        cpuReservationMHz =
                            [int64](
                                [double]$ZoneConfig.cpu_reservation_GHz *
                                1000
                            )

                        #
                        # GB -> MiB
                        #

                        memoryLimitMiB =
                            [int64](
                                [double]$ZoneConfig.memory_limit_GB *
                                1024
                            )

                        memoryReservationMiB =
                            [int64](
                                [double]$ZoneConfig.memory_reservation_GB *
                                1024
                            )
                    }
                }
        }
    }

    Write-Create (
        "VCFA regional quota '$($Body.name)'"
    )

    Invoke-ApiRest `
        -Uri (
            "https://$Server/cloudapi/v1/" +
            "virtualDatacenters"
        ) `
        -Method POST `
        -Headers $Headers `
        -Body $Body `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors |
        Out-Null


    for ($i = 0; $i -lt 60; $i++) {

        $Quota =
            Get-VcfaRegionQuota `
                -Server $Server `
                -Headers $Headers `
                -OrgId $Tenant.id `
                -RegionId $Region.id `
                -IgnoreCertificateErrors `
                    $IgnoreCertificateErrors

        if ($Quota) {

            if (
                -not (
                    Test-Property `
                        $Quota `
                        "status"
                ) -or
                $Quota.status -eq "READY"
            ) {

                return $Quota
            }

            if ($Quota.status -eq "FAILED") {

                throw (
                    "Region quota '$($Body.name)' failed."
                )
            }
        }

        Start-Sleep -Seconds 2
    }

    throw (
        "Timed out waiting for regional quota " +
        "'$($Body.name)'."
    )
}


# ============================================================
# VM Classes
# ============================================================

function Get-VcfaRegionVmClasses {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [string]$RegionId,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Values =
        Get-VcfaPagedValues `
            -Server $Server `
            -Path (
                "/cloudapi/v1/" +
                "virtualMachineClasses"
            ) `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    return $Values |
        Where-Object {

            (
                $_.region.id -eq $RegionId -or
                $_.regionRef.id -eq $RegionId
            )
        }
}


function Set-VcfaVdcVmClasses {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        $Vdc,
        $Config,
        [bool]$IgnoreCertificateErrors = $false
    )

    if (
        $Vdc.isFullAllocation
    ) {

        Write-Skip (
            "VM classes inherited because " +
            "regional quota has full allocation"
        )

        return
    }

    $RegionId =
        $Vdc.region.id

    if (-not $RegionId) {
        $RegionId =
            $Vdc.regionRef.id
    }

    $Available = @(
        Get-VcfaRegionVmClasses `
            -Server $Server `
            -Headers $Headers `
            -RegionId $RegionId `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors
    )

    $Selected = @()

    if (
        (
            Test-Property `
                $Config `
                "all_classes"
        ) -and
        [bool]$Config.all_classes
    ) {

        $Selected =
            @($Available)
    }
    else {

        foreach (
            $Name in @($Config.classes)
        ) {

            $Match =
                $Available |
                Where-Object {
                    $_.name -eq $Name
                } |
                Select-Object -First 1

            if (-not $Match) {

                throw (
                    "VM class '$Name' not found."
                )
            }

            $Selected += $Match
        }
    }

    $Body = @{
        values = @(
            $Selected |
            ForEach-Object {
                @{
                    name = $_.name
                    id   = $_.id
                }
            }
        )
    }

    Write-Info (
        "Assigning $($Body.values.Count) " +
        "VM classes to '$($Vdc.name)'"
    )

    Invoke-ApiRest `
        -Uri (
            "https://$Server/cloudapi/v1/" +
            "virtualDatacenters/$($Vdc.id)/" +
            "virtualMachineClasses"
        ) `
        -Method PUT `
        -Headers $Headers `
        -Body $Body `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors |
        Out-Null
}


# ============================================================
# Storage Classes / Region Storage Policies
# ============================================================

function Get-VcfaRegionStoragePolicies {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [string]$RegionId,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Values =
        Get-VcfaPagedValues `
            -Server $Server `
            -Path (
                "/cloudapi/v1/" +
                "regionStoragePolicies"
            ) `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors

    return $Values |
        Where-Object {

            (
                $_.region.id -eq $RegionId -or
                $_.regionRef.id -eq $RegionId
            )
        }
}


# ============================================================
# FIXED: Storage assignment requires virtualDatacenter
# inside EACH values[] entry.
# ============================================================

function Set-VcfaVdcStorageClasses {

    param (
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        $Vdc,

        [Parameter(Mandatory)]
        $Config,

        [bool]$IgnoreCertificateErrors = $false
    )


    # --------------------------------------------------------
    # Validate VDC
    # --------------------------------------------------------

    if (
        [string]::IsNullOrWhiteSpace(
            [string]$Vdc.id
        )
    ) {

        throw "VDC does not contain an id."
    }


    if (
        [string]::IsNullOrWhiteSpace(
            [string]$Vdc.name
        )
    ) {

        throw "VDC does not contain a name."
    }


    $RegionId = $null
    $RegionName = $null


    if (
        Test-Property `
            $Vdc `
            "region"
    ) {

        $RegionId =
            $Vdc.region.id

        $RegionName =
            $Vdc.region.name
    }


    if (
        -not $RegionId -and
        (
            Test-Property `
                $Vdc `
                "regionRef"
        )
    ) {

        $RegionId =
            $Vdc.regionRef.id

        $RegionName =
            $Vdc.regionRef.name
    }


    if (
        [string]::IsNullOrWhiteSpace(
            [string]$RegionId
        )
    ) {

        throw (
            "VDC '$($Vdc.name)' does not contain " +
            "a valid region reference."
        )
    }


    # --------------------------------------------------------
    # Get storage policies
    # --------------------------------------------------------

    $Available = @(
        Get-VcfaRegionStoragePolicies `
            -Server $Server `
            -Headers $Headers `
            -RegionId $RegionId `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors
    )


    if ($Available.Count -eq 0) {

        throw (
            "No storage policies found in region " +
            "'$RegionName'."
        )
    }


    # --------------------------------------------------------
    # Select policies
    # --------------------------------------------------------

    $Selected = @()


    if (
        (
            Test-Property `
                $Config `
                "all_classes"
        ) -and
        [bool]$Config.all_classes
    ) {

        $Selected =
            @($Available)


        Write-Info (
            "Using all $($Selected.Count) storage " +
            "classes for '$($Vdc.name)'"
        )
    }
    else {

        $ConfiguredClasses = @()


        if (
            Test-Property `
                $Config `
                "classes"
        ) {

            $ConfiguredClasses =
                @($Config.classes)
        }


        foreach ($Name in $ConfiguredClasses) {

            $Match =
                $Available |
                Where-Object {

                    $_.name -eq $Name -or
                    $_.kubernetesCompliantName -eq $Name

                } |
                Select-Object -First 1


            if (-not $Match) {

                throw (
                    "Storage class '$Name' not found " +
                    "in region '$RegionName'."
                )
            }


            $Selected +=
                $Match
        }
    }


    if ($Selected.Count -eq 0) {

        Write-Skip (
            "No storage classes selected for " +
            "'$($Vdc.name)'"
        )

        return
    }


    # --------------------------------------------------------
    # Build correct VCFA storage-policy assignment
    # --------------------------------------------------------

    $Values = @()


    foreach ($StoragePolicy in $Selected) {

        if (
            [string]::IsNullOrWhiteSpace(
                [string]$StoragePolicy.id
            )
        ) {

            throw (
                "Storage policy '$($StoragePolicy.name)' " +
                "does not contain an id."
            )
        }


        $Values += [ordered]@{

            #
            # REQUIRED by VCFA
            #

            virtualDatacenter = [ordered]@{
                name = $Vdc.name
                id   = $Vdc.id
            }


            regionStoragePolicy = [ordered]@{
                name = $StoragePolicy.name
                id   = $StoragePolicy.id
            }


            #
            # 0 means no explicit storage limit.
            #

            storageLimitMiB = 0
        }
    }


    $Body = [ordered]@{
        values = $Values
    }


    Write-Info (
        "Assigning $($Values.Count) storage classes " +
        "to VDC '$($Vdc.name)'"
    )


    $Uri = (
        "https://$Server/cloudapi/v1/" +
        "virtualDatacenters/$($Vdc.id)/" +
        "virtualDatacenterStoragePolicies"
    )


    try {

        Invoke-ApiRest `
            -Uri $Uri `
            -Method PUT `
            -Headers $Headers `
            -Body $Body `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors |
            Out-Null


        Write-Host (
            "[UPDATED] Storage classes for " +
            "'$($Vdc.name)'"
        ) -ForegroundColor Green
    }
    catch {

        $Status =
            Get-HttpStatusCode $_


        Write-Host ""
        Write-Host (
            "VCFA storage class assignment failed"
        ) -ForegroundColor Red


        Write-Host "VDC:      $($Vdc.name)"
        Write-Host "VDC ID:   $($Vdc.id)"
        Write-Host "HTTP:     $Status"
        Write-Host "Endpoint: $Uri"


        Write-Host ""
        Write-Host "Payload:"

        Write-Host (
            $Body |
                ConvertTo-Json -Depth 20
        )


        Write-Host ""
        Write-Host "Response:"

        Write-Host (
            Get-RestErrorDetail $_
        )


        throw
    }
}


# ============================================================
# Region Infrastructure Policies
# ============================================================

function Get-VcfaRegionInfraPolicies {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [string]$RegionId,
        [bool]$IgnoreCertificateErrors = $false
    )

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path (
            "/cloudapi/v1/regions/" +
            "$RegionId/infraPolicies"
        ) `
        -Headers $Headers `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors
}


function Set-VcfaVdcInfraPolicies {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        $Vdc,
        $Config,
        [bool]$IgnoreCertificateErrors = $false
    )

    $RegionId =
        $Vdc.region.id

    if (-not $RegionId) {
        $RegionId =
            $Vdc.regionRef.id
    }

    $Available = @(
        Get-VcfaRegionInfraPolicies `
            -Server $Server `
            -Headers $Headers `
            -RegionId $RegionId `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors
    )

    $Selected = @()

    if (
        (
            Test-Property `
                $Config `
                "all_policies"
        ) -and
        [bool]$Config.all_policies
    ) {

        $Selected =
            @($Available)
    }
    else {

        $Names = @()

        if (
            Test-Property `
                $Config `
                "policies"
        ) {

            $Names =
                @($Config.policies)
        }
        elseif (
            Test-Property `
                $Config `
                "polices"
        ) {

            #
            # Backwards compatibility for original typo
            #

            $Names =
                @($Config.polices)
        }


        foreach ($Name in $Names) {

            $Match =
                $Available |
                Where-Object {
                    $_.name -eq $Name
                } |
                Select-Object -First 1

            if (-not $Match) {

                throw (
                    "Infrastructure policy '$Name' " +
                    "not found."
                )
            }

            $Selected +=
                $Match
        }
    }


    $Body = @{
        values = @(
            $Selected |
            ForEach-Object {
                @{
                    infraPolicy = @{
                        name = $_.name
                        id   = $_.id
                    }

                    status = "ENABLED"
                }
            }
        )
    }


    Write-Info (
        "Assigning $($Body.values.Count) " +
        "infrastructure policies to " +
        "'$($Vdc.name)'"
    )


    Invoke-ApiRest `
        -Uri (
            "https://$Server/cloudapi/v1/" +
            "virtualDatacenters/$($Vdc.id)/" +
            "infraPolicies"
        ) `
        -Method PUT `
        -Headers $Headers `
        -Body $Body `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors |
        Out-Null
}


# ============================================================
# Regional Networking Settings
# ============================================================

function Get-VcfaRegionalNetworkingSettings {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [bool]$IgnoreCertificateErrors = $false
    )

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path (
            "/cloudapi/v1/" +
            "regionalNetworkingSettings"
        ) `
        -Headers $Headers `
        -PageSize 32 `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors
}


function Get-OrCreateVcfaRegionalNetworkingSetting {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        $Tenant,
        $Region,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Existing =
        Get-VcfaRegionalNetworkingSettings `
            -Server $Server `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors |
        Where-Object {

            $_.orgRef.id -eq $Tenant.id -and
            $_.regionRef.id -eq $Region.id

        } |
        Select-Object -First 1


    if ($Existing) {

        Write-Skip (
            "Regional networking setting already " +
            "exists for '$($Tenant.name)' / " +
            "'$($Region.name)'"
        )

        return $Existing
    }


    $Body = [ordered]@{

        name = (
            "$($Tenant.name)-$($Region.name)"
        )

        orgRef = @{
            name = $Tenant.name
            id   = $Tenant.id
        }

        regionRef = @{
            name = $Region.name
            id   = $Region.id
        }
    }


    Write-Create (
        "Regional networking setting " +
        "'$($Body.name)'"
    )


    Invoke-ApiRest `
        -Uri (
            "https://$Server/cloudapi/v1/" +
            "regionalNetworkingSettings"
        ) `
        -Method POST `
        -Headers $Headers `
        -Body $Body `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors |
        Out-Null


    for ($i = 0; $i -lt 60; $i++) {

        $Setting =
            Get-VcfaRegionalNetworkingSettings `
                -Server $Server `
                -Headers $Headers `
                -IgnoreCertificateErrors `
                    $IgnoreCertificateErrors |
            Where-Object {

                $_.orgRef.id -eq $Tenant.id -and
                $_.regionRef.id -eq $Region.id

            } |
            Select-Object -First 1


        if ($Setting) {
            return $Setting
        }


        Start-Sleep -Seconds 2
    }


    throw (
        "Timed out waiting for regional " +
        "networking setting."
    )
}


# ============================================================
# Distributed VLAN Connections
# ============================================================

function Get-VcfaDistributedVlanConnections {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [bool]$IgnoreCertificateErrors = $false
    )

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path (
            "/cloudapi/v1/" +
            "distributedVlanConnections"
        ) `
        -Headers $Headers `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors
}


function Set-VcfaExternalConnection {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        $Tenant,
        $Region,
        [string]$ConnectionName,
        [bool]$IgnoreCertificateErrors = $false
    )

    $Setting =
        Get-OrCreateVcfaRegionalNetworkingSetting `
            -Server $Server `
            -Headers $Headers `
            -Tenant $Tenant `
            -Region $Region `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors


    $Connection =
        Get-VcfaDistributedVlanConnections `
            -Server $Server `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors |
        Where-Object {

            $_.name -eq $ConnectionName -and
            (
                -not $_.regionRef -or
                $_.regionRef.id -eq $Region.id
            )
        } |
        Select-Object -First 1


    if (-not $Connection) {

        throw (
            "Distributed VLAN connection " +
            "'$ConnectionName' not found in region " +
            "'$($Region.name)'."
        )
    }


    $Existing =
        Invoke-ApiRest `
            -Uri (
                "https://$Server/cloudapi/v1/" +
                "regionalNetworkingSettings/" +
                "$($Setting.id)/" +
                "distributedVlanConnections"
            ) `
            -Method GET `
            -Headers $Headers `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors


    $Match =
        @($Existing.values) |
        Where-Object {

            $_.distributedVlanConnectionRef.id -eq
                $Connection.id

        } |
        Select-Object -First 1


    if ($Match) {

        Write-Skip (
            "External connection '$ConnectionName' " +
            "already assigned"
        )

        return
    }


    $Body = [ordered]@{

        distributedVlanConnectionRef = @{
            name = $Connection.name
            id   = $Connection.id
        }

        isDefault = $true
    }


    Write-Create (
        "External connection '$ConnectionName'"
    )


    Invoke-ApiRest `
        -Uri (
            "https://$Server/cloudapi/v1/" +
            "regionalNetworkingSettings/" +
            "$($Setting.id)/" +
            "distributedVlanConnections"
        ) `
        -Method POST `
        -Headers $Headers `
        -Body $Body `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors |
        Out-Null
}


# ============================================================
# Roles
# ============================================================

function Get-VcfaRoles {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [bool]$IgnoreCertificateErrors = $false
    )

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/1.0.0/roles" `
        -Headers $Headers `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors
}


# ============================================================
# Users
# ============================================================

function Get-VcfaUsers {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [bool]$IgnoreCertificateErrors = $false
    )

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/1.0.0/users" `
        -Headers $Headers `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors
}


function New-VcfaFirstUserIfMissing {

    param (
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        $Tenant,
        $UserConfig,
        [string]$ConfigDirectory,
        [bool]$IgnoreCertificateErrors = $false
    )

    $TenantHeaders =
        New-VcfaTenantHeaders `
            -Headers $Headers `
            -OrgId $Tenant.id


    $Existing =
        Get-VcfaUsers `
            -Server $Server `
            -Headers $TenantHeaders `
            -IgnoreCertificateErrors `
                $IgnoreCertificateErrors |
        Where-Object {
            $_.username -eq $UserConfig.username
        } |
        Select-Object -First 1


    if ($Existing) {

        Write-Skip (
            "VCFA user '$($UserConfig.username)' " +
            "already exists"
        )

        return
    }


    $ProviderType =
        "LOCAL"


    if (
        Test-Property `
            $UserConfig `
            "provider_type"
    ) {

        $ProviderType =
            $UserConfig.provider_type
    }


    $Body = [ordered]@{

        username =
            $UserConfig.username

        providerType =
            $ProviderType

        enabled =
            $true

        orgEntityRef = @{
            name = $Tenant.name
            id   = $Tenant.id
        }
    }


    if (
        Test-Property `
            $UserConfig `
            "enabled"
    ) {

        $Body.enabled =
            [bool]$UserConfig.enabled
    }


    if ($ProviderType -eq "LOCAL") {

        if (
            -not (
                Test-Property `
                    $UserConfig `
                    "password_file"
            )
        ) {

            throw (
                "LOCAL user '$($UserConfig.username)' " +
                "requires password_file."
            )
        }


        $UserPasswordFile =
            Resolve-ConfigFilePath `
                -Path $UserConfig.password_file `
                -ConfigDirectory $ConfigDirectory


        if (
            -not (
                Test-Path $UserPasswordFile
            )
        ) {

            throw (
                "User password file not found: " +
                "$UserPasswordFile"
            )
        }


        $Body.password = (
            Get-Content `
                -Path $UserPasswordFile `
                -Raw
        ).Trim()
    }


    if (
        Test-Property `
            $UserConfig `
            "role"
    ) {

        $Role =
            Get-VcfaRoles `
                -Server $Server `
                -Headers $TenantHeaders `
                -IgnoreCertificateErrors `
                    $IgnoreCertificateErrors |
            Where-Object {
                $_.name -eq $UserConfig.role
            } |
            Select-Object -First 1


        if (-not $Role) {

            throw (
                "VCFA role '$($UserConfig.role)' " +
                "not found."
            )
        }


        $Body.roleEntityRefs = @(
            @{
                name = $Role.name
                id   = $Role.id
            }
        )
    }


    Write-Create (
        "VCFA user '$($UserConfig.username)'"
    )


    Invoke-ApiRest `
        -Uri (
            "https://$Server/cloudapi/1.0.0/users"
        ) `
        -Method POST `
        -Headers $TenantHeaders `
        -Body $Body `
        -IgnoreCertificateErrors `
            $IgnoreCertificateErrors |
        Out-Null
}


# ============================================================
# Load Config
# ============================================================

$ResolvedConfig = (
    Resolve-Path $ConfigFile
).Path

$ConfigDirectory =
    Split-Path `
        -Parent `
        $ResolvedConfig


Write-Info (
    "Loading '$ResolvedConfig'"
)


$Config =
    Get-Content `
        -Path $ResolvedConfig `
        -Raw |
    ConvertFrom-Json


# ============================================================
# Validate vCenter
# ============================================================

if (
    -not (
        Test-Property `
            $Config `
            "vcenter"
    )
) {

    throw (
        "vcenter section is required."
    )
}


if (-not $Config.vcenter.server) {
    throw "vcenter.server is required."
}


if (-not $Config.vcenter.username) {
    throw "vcenter.username is required."
}


if (-not $Config.vcenter.password_file) {
    throw "vcenter.password_file is required."
}


# ============================================================
# vCenter credentials
# ============================================================

$PasswordFile =
    Resolve-ConfigFilePath `
        -Path $Config.vcenter.password_file `
        -ConfigDirectory $ConfigDirectory


if (-not (Test-Path $PasswordFile)) {

    throw (
        "vCenter password file not found: " +
        "$PasswordFile"
    )
}


$Password = (
    Get-Content `
        -Path $PasswordFile `
        -Raw
).Trim()


$SecurePassword =
    ConvertTo-SecureString `
        -String $Password `
        -AsPlainText `
        -Force


$Credential =
    [PSCredential]::new(
        $Config.vcenter.username,
        $SecurePassword
    )


$IgnoreCertErrors = $false


if (
    Test-Property `
        $Config.vcenter `
        "ignore_certificate_errors"
) {

    $IgnoreCertErrors =
        [bool]$Config.vcenter.
            ignore_certificate_errors
}


if ($IgnoreCertErrors) {

    Set-PowerCLIConfiguration `
        -InvalidCertificateAction Ignore `
        -Confirm:$false |
        Out-Null
}


# ============================================================
# Optional VCFA
# ============================================================

$VcfaConfigured = (
    Test-Property `
        $Config `
        "vcfa"
) -and
($null -ne $Config.vcfa) -and
-not [string]::IsNullOrWhiteSpace(
    [string]$Config.vcfa.server
)


$VcfaHeaders = $null
$VcfaIgnoreCertErrors = $false


if ($VcfaConfigured) {

    if (
        -not $Config.vcfa.api_token_file
    ) {

        throw (
            "vcfa.api_token_file is required."
        )
    }


    if (
        Test-Property `
            $Config.vcfa `
            "ignore_certificate_errors"
    ) {

        $VcfaIgnoreCertErrors =
            [bool]$Config.vcfa.
                ignore_certificate_errors
    }


    $TokenFile =
        Resolve-ConfigFilePath `
            -Path $Config.vcfa.api_token_file `
            -ConfigDirectory $ConfigDirectory


    $ApiToken =
        Get-VcfaApiToken `
            -TokenFile $TokenFile


    $AccessToken =
        Get-VcfaAccessToken `
            -Server $Config.vcfa.server `
            -ApiToken $ApiToken `
            -IgnoreCertificateErrors `
                $VcfaIgnoreCertErrors


    $VcfaHeaders =
        New-VcfaHeaders `
            -AccessToken $AccessToken


    $ApiToken = $null
}
else {

    Write-Skip (
        "VCFA configuration not provided"
    )
}


# ============================================================
# Main
# ============================================================

$VIServer = $null
$RestHeaders = $null


try {

    # ========================================================
    # Connect vCenter
    # ========================================================

    Write-Info (
        "Connecting to '$($Config.vcenter.server)'"
    )


    $VIServer =
        Connect-VIServer `
            -Server $Config.vcenter.server `
            -Credential $Credential `
            -ErrorAction Stop


    $RestHeaders =
        New-VCenterRestSession `
            -Server $Config.vcenter.server `
            -Username $Config.vcenter.username `
            -Password $Password `
            -IgnoreCertificateErrors `
                $IgnoreCertErrors


    # ========================================================
    # Categories / Tags - OPTIONAL
    # ========================================================

    if (
        (
            Test-Property `
                $Config `
                "categories"
        ) -and
        $Config.categories -and
        (
            @($Config.categories).Count `
                -gt 0
        )
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " vCenter Categories / Tags"
        Write-Host "========================================"


        foreach (
            $CategoryConfig in
            @($Config.categories)
        ) {

            $Description = ""


            if (
                Test-Property `
                    $CategoryConfig `
                    "description"
            ) {

                $Description =
                    [string]$CategoryConfig.description
            }


            $Cardinality =
                "Single"


            if (
                Test-Property `
                    $CategoryConfig `
                    "cardinality"
            ) {

                $Cardinality =
                    [string]$CategoryConfig.cardinality
            }


            $EntityTypes =
                @()


            if (
                Test-Property `
                    $CategoryConfig `
                    "entity_types"
            ) {

                $EntityTypes =
                    @(
                        $CategoryConfig.entity_types
                    )
            }


            $Category =
                Get-OrCreateTagCategory `
                    -Name $CategoryConfig.name `
                    -Description $Description `
                    -Cardinality $Cardinality `
                    -EntityTypes $EntityTypes


            if (
                Test-Property `
                    $CategoryConfig `
                    "tags"
            ) {

                foreach (
                    $TagConfig in
                    @($CategoryConfig.tags)
                ) {

                    $TagDescription = ""


                    if (
                        Test-Property `
                            $TagConfig `
                            "description"
                    ) {

                        $TagDescription =
                            [string]$TagConfig.description
                    }


                    Get-OrCreateTag `
                        -Name $TagConfig.name `
                        -Category $Category `
                        -Description $TagDescription |
                        Out-Null
                }
            }
        }
    }
    else {

        Write-Skip (
            "No vCenter categories configured"
        )
    }


    # ========================================================
    # Tag Assignments - OPTIONAL
    # ========================================================

    if (
        (
            Test-Property `
                $Config `
                "assignments"
        ) -and
        $Config.assignments -and
        (
            @($Config.assignments).Count `
                -gt 0
        )
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " vCenter Tag Assignments"
        Write-Host "========================================"


        foreach (
            $Assignment in
            @($Config.assignments)
        ) {

            $Entity =
                Get-vSphereObject `
                    -Type $Assignment.type `
                    -Name $Assignment.name


            foreach (
                $TagConfig in
                @($Assignment.tags)
            ) {

                $Tag =
                    Get-ExactTag `
                        -CategoryName `
                            $TagConfig.category `
                        -TagName `
                            $TagConfig.tag


                if (-not $Tag) {

                    throw (
                        "Tag '$($TagConfig.category)/" +
                        "$($TagConfig.tag)' does not exist."
                    )
                }


                Set-TagIfMissing `
                    -Entity $Entity `
                    -Tag $Tag
            }
        }
    }
    else {

        Write-Skip (
            "No vCenter tag assignments configured"
        )
    }


    # ========================================================
    # vCenter Compute Policies - OPTIONAL
    # ========================================================

    if (
        (
            Test-Property `
                $Config `
                "policies"
        ) -and
        $Config.policies -and
        (
            @($Config.policies).Count `
                -gt 0
        )
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " vCenter Compute Policies"
        Write-Host "========================================"


        foreach (
            $Policy in
            @($Config.policies)
        ) {

            New-RestComputePolicyIfMissing `
                -Server `
                    $Config.vcenter.server `
                -Headers `
                    $RestHeaders `
                -Policy `
                    $Policy `
                -IgnoreCertificateErrors `
                    $IgnoreCertErrors
        }
    }
    else {

        Write-Skip (
            "No vCenter compute policies configured"
        )
    }


    # ========================================================
    # VCFA Infrastructure Policies - OPTIONAL
    # ========================================================

    if (
        $VcfaConfigured -and
        (
            Test-Property `
                $Config `
                "infrastructure_policies"
        ) -and
        $Config.infrastructure_policies -and
        (
            @($Config.infrastructure_policies).Count `
                -gt 0
        )
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " VCFA Infrastructure Policies"
        Write-Host "========================================"


        foreach (
            $Policy in
            @($Config.infrastructure_policies)
        ) {

            if (
                Test-Property `
                    $Policy `
                    "vc_compute_policy_name"
            ) {

                $VCPolicy =
                    Get-RestComputePolicy `
                        -Server `
                            $Config.vcenter.server `
                        -Headers `
                            $RestHeaders `
                        -Name `
                            $Policy.vc_compute_policy_name `
                        -IgnoreCertificateErrors `
                            $IgnoreCertErrors


                if (-not $VCPolicy) {

                    throw (
                        "Referenced vCenter compute policy " +
                        "'$($Policy.vc_compute_policy_name)' " +
                        "does not exist."
                    )
                }
            }


            New-VcfaInfrastructurePolicyIfMissing `
                -Server `
                    $Config.vcfa.server `
                -Headers `
                    $VcfaHeaders `
                -Policy `
                    $Policy `
                -IgnoreCertificateErrors `
                    $VcfaIgnoreCertErrors
        }
    }
    elseif ($VcfaConfigured) {

        Write-Skip (
            "No VCFA infrastructure policies configured"
        )
    }


    # ========================================================
    # VCFA Tenants - OPTIONAL
    # ========================================================

    if (
        $VcfaConfigured -and
        (
            Test-Property `
                $Config `
                "tenants"
        ) -and
        $Config.tenants -and
        (
            @($Config.tenants).Count `
                -gt 0
        )
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " VCFA Tenants"
        Write-Host "========================================"


        foreach (
            $TenantConfig in
            @($Config.tenants)
        ) {

            # ------------------------------------------------
            # Tenant
            # ------------------------------------------------

            $Tenant =
                Get-OrCreateVcfaTenant `
                    -Server `
                        $Config.vcfa.server `
                    -Headers `
                        $VcfaHeaders `
                    -Tenant `
                        $TenantConfig `
                    -IgnoreCertificateErrors `
                        $VcfaIgnoreCertErrors


            Write-Info (
                "Tenant '$($Tenant.name)' " +
                "[$($Tenant.id)]"
            )


            # ------------------------------------------------
            # Regional Quota
            # ------------------------------------------------

            $RegionQuota = $null
            $Region = $null


            if (
                Test-Property `
                    $TenantConfig `
                    "regional_quota"
            ) {

                $RegionQuota =
                    New-VcfaRegionQuotaIfMissing `
                        -Server `
                            $Config.vcfa.server `
                        -Headers `
                            $VcfaHeaders `
                        -Tenant `
                            $Tenant `
                        -QuotaConfig `
                            $TenantConfig.regional_quota `
                        -IgnoreCertificateErrors `
                            $VcfaIgnoreCertErrors


                $Region =
                    Get-VcfaRegion `
                        -Server `
                            $Config.vcfa.server `
                        -Headers `
                            $VcfaHeaders `
                        -Name `
                            $TenantConfig.regional_quota.region `
                        -IgnoreCertificateErrors `
                            $VcfaIgnoreCertErrors
            }
            else {

                Write-Skip (
                    "No regional quota configured " +
                    "for '$($Tenant.name)'"
                )
            }


            # ------------------------------------------------
            # Region Resources
            # ------------------------------------------------

            if (
                $RegionQuota -and
                (
                    Test-Property `
                        $TenantConfig `
                        "resources"
                )
            ) {

                $Resources =
                    $TenantConfig.resources


                # --------------------------------------------
                # VM Classes
                # --------------------------------------------

                if (
                    Test-Property `
                        $Resources `
                        "vm_classes"
                ) {

                    Set-VcfaVdcVmClasses `
                        -Server `
                            $Config.vcfa.server `
                        -Headers `
                            $VcfaHeaders `
                        -Vdc `
                            $RegionQuota `
                        -Config `
                            $Resources.vm_classes `
                        -IgnoreCertificateErrors `
                            $VcfaIgnoreCertErrors
                }


                # --------------------------------------------
                # Storage Classes
                # --------------------------------------------

                if (
                    Test-Property `
                        $Resources `
                        "storage_classes"
                ) {

                    Set-VcfaVdcStorageClasses `
                        -Server `
                            $Config.vcfa.server `
                        -Headers `
                            $VcfaHeaders `
                        -Vdc `
                            $RegionQuota `
                        -Config `
                            $Resources.storage_classes `
                        -IgnoreCertificateErrors `
                            $VcfaIgnoreCertErrors
                }


                # --------------------------------------------
                # Infrastructure Policies
                # --------------------------------------------

                if (
                    Test-Property `
                        $Resources `
                        "infra_policies"
                ) {

                    Set-VcfaVdcInfraPolicies `
                        -Server `
                            $Config.vcfa.server `
                        -Headers `
                            $VcfaHeaders `
                        -Vdc `
                            $RegionQuota `
                        -Config `
                            $Resources.infra_policies `
                        -IgnoreCertificateErrors `
                            $VcfaIgnoreCertErrors
                }
            }


            # ------------------------------------------------
            # External Distributed VLAN Connection
            # ------------------------------------------------

            if (
                $Region -and
                (
                    Test-Property `
                        $TenantConfig `
                        "external_connection"
                ) -and
                -not [string]::IsNullOrWhiteSpace(
                    [string]$TenantConfig.external_connection
                )
            ) {

                Set-VcfaExternalConnection `
                    -Server `
                        $Config.vcfa.server `
                    -Headers `
                        $VcfaHeaders `
                    -Tenant `
                        $Tenant `
                    -Region `
                        $Region `
                    -ConnectionName `
                        $TenantConfig.external_connection `
                    -IgnoreCertificateErrors `
                        $VcfaIgnoreCertErrors
            }


            # ------------------------------------------------
            # First User
            # ------------------------------------------------

            if (
                Test-Property `
                    $TenantConfig `
                    "first_user"
            ) {

                New-VcfaFirstUserIfMissing `
                    -Server `
                        $Config.vcfa.server `
                    -Headers `
                        $VcfaHeaders `
                    -Tenant `
                        $Tenant `
                    -UserConfig `
                        $TenantConfig.first_user `
                    -ConfigDirectory `
                        $ConfigDirectory `
                    -IgnoreCertificateErrors `
                        $VcfaIgnoreCertErrors
            }
            else {

                Write-Skip (
                    "No first user configured for " +
                    "'$($Tenant.name)'"
                )
            }
        }
    }
    elseif ($VcfaConfigured) {

        Write-Skip (
            "No VCFA tenants configured"
        )
    }


    Write-Host ""
    Write-Host (
        "Configuration completed successfully."
    ) -ForegroundColor Green
}
finally {

    # ========================================================
    # Close REST session
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