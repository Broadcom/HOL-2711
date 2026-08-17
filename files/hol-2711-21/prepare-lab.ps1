#Requires -Modules VMware.VimAutomation.Core

param (
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateScript({
        if (-not (Test-Path $_ -PathType Leaf)) { throw "Configuration file does not exist: $_" }
        if ([System.IO.Path]::GetExtension($_) -ne ".json") { throw "Configuration file must be a .json file: $_" }
        return $true
    })]
    [string]$ConfigFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Info   { param([string]$Message) Write-Host "[INFO]   $Message" -ForegroundColor Cyan }
function Write-Create { param([string]$Message) Write-Host "[CREATE] $Message" -ForegroundColor Green }
function Write-Skip   { param([string]$Message) Write-Host "[SKIP]   $Message" -ForegroundColor DarkGray }
function Write-Warn   { param([string]$Message) Write-Host "[WARN]   $Message" -ForegroundColor Yellow }

function Test-Property {

    param(
        [object]$Object,
        [Parameter(Mandatory)]
        [string]$Name
    )

    if ($null -eq $Object) {
        return $false
    }

    if ($Object -is [System.Collections.IDictionary]) {
        return $Object.Contains($Name)
    }

    try {
        return ($null -ne $Object.PSObject.Properties[$Name])
    }
    catch {
        return $false
    }
}


function Get-PropertyValue {

    param(
        [object]$Object,
        [Parameter(Mandatory)]
        [string[]]$Names,
        $Default = $null
    )

    if ($null -eq $Object) {
        return $Default
    }

    foreach ($PropertyName in $Names) {

        if (Test-Property $Object $PropertyName) {

            if ($Object -is [System.Collections.IDictionary]) {
                $Value = $Object[$PropertyName]
            }
            else {
                $Value = $Object.PSObject.Properties[$PropertyName].Value
            }

            if ($null -ne $Value) {
                return $Value
            }
        }
    }

    return $Default
}

function Get-VcfaObjectReference {
    param([Parameter(Mandatory)][object]$Object,[Parameter(Mandatory)][string[]]$PropertyNames)
    foreach ($PropertyName in $PropertyNames) {
        if (-not (Test-Property $Object $PropertyName)) { continue }
        $Reference = Get-PropertyValue -Object $Object -Names @($PropertyName)
        if ($null -eq $Reference) { continue }
        $Id = $null; $Name = $null
        if (Test-Property $Reference "id") { $Id = [string](Get-PropertyValue -Object $Reference -Names @("id")) }
        elseif (Test-Property $Reference "urn") { $Id = [string](Get-PropertyValue -Object $Reference -Names @("urn")) }
        if (Test-Property $Reference "name") { $Name = [string](Get-PropertyValue -Object $Reference -Names @("name")) }
        elseif (Test-Property $Reference "displayName") { $Name = [string](Get-PropertyValue -Object $Reference -Names @("displayName")) }
        if ($Id -or $Name) { return [PSCustomObject]@{ Id = $Id; Name = $Name } }
    }
    return $null
}

function Get-VcfaObjectId {
    param([Parameter(Mandatory)][object]$Object)
    foreach ($Name in @("id","urn","supervisorId","zoneId","regionId")) {
        if (Test-Property $Object $Name) {
            $Value = Get-PropertyValue -Object $Object -Names @($Name)
            if (-not [string]::IsNullOrWhiteSpace([string]$Value)) { return [string]$Value }
        }
    }
    return $null
}

function Get-HttpStatusCode {
    param([Parameter(Mandatory)]$ErrorRecord)
    try { if ($ErrorRecord.Exception.Response.StatusCode) { return [int]$ErrorRecord.Exception.Response.StatusCode } } catch {}
    return $null
}

function Get-RestErrorDetail {
    param([Parameter(Mandatory)]$ErrorRecord)
    $Message = $ErrorRecord.Exception.Message
    if ($ErrorRecord.ErrorDetails -and $ErrorRecord.ErrorDetails.Message) { $Message += "`n$($ErrorRecord.ErrorDetails.Message)" }
    return $Message
}

function Invoke-ApiRest {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][ValidateSet("GET","POST","PUT","PATCH","DELETE")][string]$Method,
        [System.Collections.IDictionary]$Headers,
        [object]$Body,
        [string]$ContentType = "application/json",
        [bool]$IgnoreCertificateErrors = $false
    )
    $Params = @{ Uri=$Uri; Method=$Method; ErrorAction="Stop" }
    if ($null -ne $Headers) { $Params.Headers = $Headers }
    if ($null -ne $Body) {
        $Params.ContentType = $ContentType
        if ($Body -is [string]) { $Params.Body = $Body }
        else { $Params.Body = $Body | ConvertTo-Json -Depth 50 }
    }
    $IRM = Get-Command Invoke-RestMethod
    if ($IgnoreCertificateErrors -and $IRM.Parameters.ContainsKey("SkipCertificateCheck")) { $Params.SkipCertificateCheck = $true }
    Invoke-RestMethod @Params
}

function Resolve-ConfigFilePath {
    param([string]$Path,[string]$ConfigDirectory)
    if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
    return Join-Path -Path $ConfigDirectory -ChildPath $Path
}

function Get-VcfaPagedValues {
    param([string]$Server,[string]$Path,[System.Collections.IDictionary]$Headers,[int]$PageSize=128,[bool]$IgnoreCertificateErrors=$false)
    $Values=@(); $Page=1
    do {
        $Separator = if ($Path.Contains("?")) { "&" } else { "?" }
        $Uri = "https://$Server$Path${Separator}page=$Page&pageSize=$PageSize"
        $Result = Invoke-ApiRest -Uri $Uri -Method GET -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors
        if ($Result -and (Test-Property $Result "values") -and $Result.values) { $Values += @($Result.values) }
        elseif ($Result -is [System.Array]) { $Values += @($Result) }
        $PageCount=1
        if ($Result -and (Test-Property $Result "pageCount") -and $Result.pageCount) { $PageCount=[int]$Result.pageCount }
        $Page++
    } while ($Page -le $PageCount)
    return $Values
}

function New-VCenterRestSession {
    param([string]$Server,[string]$Username,[string]$Password,[bool]$IgnoreCertificateErrors=$false)
    Write-Info "Creating vCenter REST session"
    $Basic=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${Username}:${Password}"))
    $AuthHeaders=[System.Collections.Generic.Dictionary[string,string]]::new(); $AuthHeaders.Add("Authorization","Basic $Basic")
    $SessionId=Invoke-ApiRest -Uri "https://$Server/api/session" -Method POST -Headers $AuthHeaders -IgnoreCertificateErrors $IgnoreCertificateErrors
    if ([string]::IsNullOrWhiteSpace([string]$SessionId)) { throw "vCenter REST authentication failed." }
    $Headers=[System.Collections.Generic.Dictionary[string,string]]::new(); $Headers.Add("vmware-api-session-id",[string]$SessionId)
    return $Headers
}

function Get-ExactTagCategory { param([string]$Name) return Get-TagCategory | Where-Object {$_.Name -eq $Name} | Select-Object -First 1 }
function Get-OrCreateTagCategory {
    param([string]$Name,[string]$Description="",[ValidateSet("Single","Multiple")][string]$Cardinality="Single",[string[]]$EntityTypes)
    $Category=Get-ExactTagCategory -Name $Name
    if ($Category) { Write-Skip "Category '$Name' already exists"; return $Category }
    Write-Create "Category '$Name'"
    $Params=@{Name=$Name;Description=$Description;Cardinality=$Cardinality}
    if ($EntityTypes -and $EntityTypes.Count -gt 0) { $Params.EntityType=$EntityTypes }
    return New-TagCategory @Params
}
function Get-ExactTag {
    param([string]$CategoryName,[string]$TagName)
    return Get-Tag | Where-Object {$_.Name -eq $TagName -and $_.Category.Name -eq $CategoryName} | Select-Object -First 1
}
function Get-OrCreateTag {
    param([string]$Name,$Category,[string]$Description="")
    $Tag=Get-ExactTag -CategoryName $Category.Name -TagName $Name
    if ($Tag) { Write-Skip "Tag '$($Category.Name)/$Name' already exists"; return $Tag }
    Write-Create "Tag '$($Category.Name)/$Name'"
    return New-Tag -Name $Name -Category $Category -Description $Description
}
function Get-vSphereObject {
    param([string]$Type,[string]$Name)
    switch ($Type.ToLower()) {
        "virtualmachine" { return Get-VM -Name $Name -ErrorAction Stop }
        "vm" { return Get-VM -Name $Name -ErrorAction Stop }
        "vmhost" { return Get-VMHost -Name $Name -ErrorAction Stop }
        "cluster" { return Get-Cluster -Name $Name -ErrorAction Stop }
        "datacenter" { return Get-Datacenter -Name $Name -ErrorAction Stop }
        "datastore" { return Get-Datastore -Name $Name -ErrorAction Stop }
        "datastorecluster" { return Get-DatastoreCluster -Name $Name -ErrorAction Stop }
        "resourcepool" { return Get-ResourcePool -Name $Name -ErrorAction Stop }
        "folder" { return Get-Folder -Name $Name -ErrorAction Stop }
        "vapp" { return Get-VApp -Name $Name -ErrorAction Stop }
        default { throw "Unsupported object type '$Type'" }
    }
}
function Set-TagIfMissing {
    param($Entity,$Tag)
    $Existing=Get-TagAssignment -Entity $Entity -ErrorAction SilentlyContinue | Where-Object {$_.Tag.Name -eq $Tag.Name -and $_.Tag.Category.Name -eq $Tag.Category.Name} | Select-Object -First 1
    if ($Existing) { Write-Skip "'$($Entity.Name)' already has '$($Tag.Category.Name)/$($Tag.Name)'"; return }
    Write-Create "Assign '$($Tag.Category.Name)/$($Tag.Name)' to '$($Entity.Name)'"
    New-TagAssignment -Entity $Entity -Tag $Tag | Out-Null
}

function Get-RestCategoryId {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[string]$CategoryName,[bool]$IgnoreCertificateErrors=$false)
    $Result=Invoke-ApiRest -Uri "https://$Server/api/vcenter/tagging/categories" -Method GET -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors
    $Category=$Result.items | Where-Object {$_.info.name -eq $CategoryName} | Select-Object -First 1
    if (-not $Category) { throw "REST category '$CategoryName' not found." }
    return $Category.category_id
}
function Get-RestTagId {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[string]$CategoryName,[string]$TagName,[bool]$IgnoreCertificateErrors=$false)
    $CategoryId=Get-RestCategoryId -Server $Server -Headers $Headers -CategoryName $CategoryName -IgnoreCertificateErrors $IgnoreCertificateErrors
    $Result=Invoke-ApiRest -Uri "https://$Server/api/vcenter/tagging/tags" -Method GET -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors
    $Tag=$Result.items | Where-Object {$_.info.name -eq $TagName -and $_.info.category -eq $CategoryId} | Select-Object -First 1
    if (-not $Tag) { throw "REST tag '$CategoryName/$TagName' not found." }
    return $Tag.tag
}
function Get-RestComputePolicy {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[string]$Name,[bool]$IgnoreCertificateErrors=$false)
    $Policies=Invoke-ApiRest -Uri "https://$Server/api/vcenter/compute/policies" -Method GET -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors
    return $Policies | Where-Object {$_.name -eq $Name} | Select-Object -First 1
}
function Get-RestComputePolicyCapability {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[ValidateSet("vm-host-affinity","vm-host-anti-affinity")][string]$Type,[bool]$IgnoreCertificateErrors=$false)
    $Capabilities=Invoke-ApiRest -Uri "https://$Server/api/vcenter/compute/policies/capabilities" -Method GET -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors
    switch ($Type) {
        "vm-host-affinity" { $Capability=$Capabilities | Where-Object {$_.capability -match "VmHostAffinity" -or ($_.name -match "host" -and $_.name -match "affinity" -and $_.name -notmatch "anti")} | Select-Object -First 1 }
        "vm-host-anti-affinity" { $Capability=$Capabilities | Where-Object {$_.capability -match "VmHostAntiAffinity" -or ($_.name -match "host" -and $_.name -match "anti")} | Select-Object -First 1 }
    }
    if (-not $Capability) { throw "Compute policy capability '$Type' not found." }
    return $Capability
}
function New-RestComputePolicyIfMissing {
    param([string]$Server,[System.Collections.IDictionary]$Headers,$Policy,[bool]$IgnoreCertificateErrors=$false)
    $Existing=Get-RestComputePolicy -Server $Server -Headers $Headers -Name $Policy.name -IgnoreCertificateErrors $IgnoreCertificateErrors
    if ($Existing) { Write-Skip "vCenter compute policy '$($Policy.name)' already exists"; return }
    $VMTagId=Get-RestTagId -Server $Server -Headers $Headers -CategoryName $Policy.vm_tag.category -TagName $Policy.vm_tag.tag -IgnoreCertificateErrors $IgnoreCertificateErrors
    $HostTagId=Get-RestTagId -Server $Server -Headers $Headers -CategoryName $Policy.host_tag.category -TagName $Policy.host_tag.tag -IgnoreCertificateErrors $IgnoreCertificateErrors
    $Capability=Get-RestComputePolicyCapability -Server $Server -Headers $Headers -Type $Policy.type -IgnoreCertificateErrors $IgnoreCertificateErrors
    $Body=[ordered]@{capability=$Capability.capability;name=$Policy.name;description=$Policy.description;vm_tag=$VMTagId;host_tag=$HostTagId}
    Write-Create "vCenter compute policy '$($Policy.name)'"
    Invoke-ApiRest -Uri "https://$Server/api/vcenter/compute/policies" -Method POST -Headers $Headers -Body $Body -IgnoreCertificateErrors $IgnoreCertificateErrors | Out-Null
}

function Get-VcfaApiToken {
    param([string]$TokenFile)
    if (-not (Test-Path $TokenFile)) { throw "VCFA API token file not found: $TokenFile" }
    $Raw=(Get-Content -Path $TokenFile -Raw).Trim()
    if ($Raw.StartsWith("{")) {
        $Object=$Raw | ConvertFrom-Json
        foreach ($Property in @("refresh_token","api_token","token")) {
            if ($Object.PSObject.Properties.Name -contains $Property) { return ([string]$Object.$Property).Trim() }
        }
        throw "VCFA token JSON contains no supported token."
    }
    return $Raw
}
function Get-VcfaAccessToken {
    param([string]$Server,[string]$ApiToken,[bool]$IgnoreCertificateErrors=$false)
    Write-Info "Exchanging VCFA API token for bearer token"
    $Body="grant_type=refresh_token&refresh_token=" + [uri]::EscapeDataString($ApiToken)
    $Headers=[System.Collections.Generic.Dictionary[string,string]]::new(); $Headers.Add("Accept","application/json")
    $Result=Invoke-ApiRest -Uri "https://$Server/oauth/provider/token" -Method POST -Headers $Headers -Body $Body -ContentType "application/x-www-form-urlencoded" -IgnoreCertificateErrors $IgnoreCertificateErrors
    if (-not $Result.access_token) { throw "VCFA access token was not returned." }
    return [string]$Result.access_token
}
function New-VcfaHeaders {
    param([string]$AccessToken)
    $Headers=[System.Collections.Generic.Dictionary[string,string]]::new(); $Headers.Add("Authorization","Bearer $AccessToken"); $Headers.Add("Accept","application/json;version=9.1.0"); return $Headers
}
function Get-VcfaVCenterComputePolicies {
    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [bool]$IgnoreCertificateErrors = $false
    )

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/v1/vCenterComputePolicies" `
        -Headers $Headers `
        -PageSize 128 `
        -IgnoreCertificateErrors $IgnoreCertificateErrors
}

function Wait-VcfaVCenterComputePolicy {
    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        [string]$PolicyName,

        [string]$VCenterName,

        [int]$TimeoutSeconds = 300,

        [int]$PollIntervalSeconds = 10,

        [bool]$IgnoreCertificateErrors = $false
    )

    if ($TimeoutSeconds -lt 1) {
        throw "TimeoutSeconds must be greater than zero."
    }

    if ($PollIntervalSeconds -lt 1) {
        throw "PollIntervalSeconds must be greater than zero."
    }

    Write-Info (
        "Waiting for VCFA to discover vCenter compute policy " +
        "'$PolicyName'"
    )

    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $Attempt = 0

    do {
        $Attempt++

        try {
            $Policies = @(
                Get-VcfaVCenterComputePolicies `
                    -Server $Server `
                    -Headers $Headers `
                    -IgnoreCertificateErrors $IgnoreCertificateErrors
            )

            $Match = $null

            foreach ($Candidate in $Policies) {
                if (-not (Test-Property $Candidate "name")) {
                    continue
                }

                if ([string]$Candidate.name -ne $PolicyName) {
                    continue
                }

                if (-not [string]::IsNullOrWhiteSpace($VCenterName)) {
                    $CandidateVCenter = $null

                    if (Test-Property $Candidate "vcenter") {
                        $VCenterRef = $Candidate.vcenter

                        if ($null -ne $VCenterRef -and (Test-Property $VCenterRef "name")) {
                            $CandidateVCenter = [string]$VCenterRef.name
                        }
                    }

                    if (
                        -not [string]::IsNullOrWhiteSpace($CandidateVCenter) -and
                        $CandidateVCenter -ne $VCenterName
                    ) {
                        continue
                    }
                }

                $Match = $Candidate
                break
            }

            if ($Match) {
                $PolicyId = ""
                if (Test-Property $Match "id") {
                    $PolicyId = [string]$Match.id
                }

                if ([string]::IsNullOrWhiteSpace($PolicyId)) {
                    Write-Info (
                        "VCFA discovered compute policy '$PolicyName' " +
                        "after $Attempt check(s)"
                    )
                }
                else {
                    Write-Info (
                        "VCFA discovered compute policy '$PolicyName' " +
                        "[$PolicyId] after $Attempt check(s)"
                    )
                }

                return $Match
            }
        }
        catch {
            Write-Warn (
                "VCFA compute-policy discovery check $Attempt failed: " +
                "$($_.Exception.Message)"
            )
        }

        if ((Get-Date) -ge $Deadline) {
            break
        }

        Write-Info (
            "VCFA has not discovered '$PolicyName' yet; " +
            "checking again in $PollIntervalSeconds seconds"
        )

        Start-Sleep -Seconds $PollIntervalSeconds

    } while ((Get-Date) -lt $Deadline)

    throw (
        "Timed out after $TimeoutSeconds seconds waiting for VCFA " +
        "to discover vCenter compute policy '$PolicyName'. " +
        "The policy exists in vCenter but is not yet visible from " +
        "VCFA /cloudapi/v1/vCenterComputePolicies."
    )
}

function Get-VcfaInfrastructurePolicies {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[bool]$IgnoreCertificateErrors=$false)
    return Get-VcfaPagedValues -Server $Server -Path "/cloudapi/v1/infraPolicies" -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors
}
function New-VcfaInfrastructurePolicyIfMissing {
    param([string]$Server,[System.Collections.IDictionary]$Headers,$Policy,[bool]$IgnoreCertificateErrors=$false)
    $Existing=Get-VcfaInfrastructurePolicies -Server $Server -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors | Where-Object {$_.name -eq $Policy.name} | Select-Object -First 1
    if ($Existing) { Write-Skip "VCFA infrastructure policy '$($Policy.name)' already exists"; return $Existing }
    $Body=[ordered]@{name=$Policy.name}
    if (Test-Property $Policy "description") { $Body.description=$Policy.description }
    if (Test-Property $Policy "vc_compute_policy_name") { $Body.vcComputePolicyName=$Policy.vc_compute_policy_name }
    if (Test-Property $Policy "is_mandatory") { $Body.isMandatory=[bool]$Policy.is_mandatory }
    Write-Create "VCFA infrastructure policy '$($Policy.name)'"
    Invoke-ApiRest -Uri "https://$Server/cloudapi/v1/infraPolicies" -Method POST -Headers $Headers -Body $Body -IgnoreCertificateErrors $IgnoreCertificateErrors | Out-Null
}


# ============================================================
# VCFA Provider Content Libraries
#
# A content library created in System context is a Provider
# Content Library. Creation is asynchronous and returns 202.
# ============================================================

function New-VcfaSystemHeaders {
    param(
        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers
    )

    $Result =
        [System.Collections.Generic.Dictionary[string,string]]::new()

    foreach ($Key in $Headers.Keys) {
        $Result.Add(
            [string]$Key,
            [string]$Headers[$Key]
        )
    }

    $Result["X-VMWARE-VCLOUD-AUTH-CONTEXT"] = "System"

    if ($Result.ContainsKey("X-VMWARE-VCLOUD-TENANT-CONTEXT")) {
        $Result.Remove("X-VMWARE-VCLOUD-TENANT-CONTEXT")
    }

    return $Result
}


function Get-VcfaContentLibraries {

    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [bool]$IgnoreCertificateErrors = $false
    )

    $SystemHeaders =
        New-VcfaSystemHeaders `
            -Headers $Headers

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/v1/contentLibraries" `
        -Headers $SystemHeaders `
        -PageSize 64 `
        -IgnoreCertificateErrors $IgnoreCertificateErrors
}


function Get-VcfaProviderContentLibrary {

    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        [string]$Name,

        [bool]$IgnoreCertificateErrors = $false
    )

    $Libraries = @(
        Get-VcfaContentLibraries `
            -Server $Server `
            -Headers $Headers `
            -IgnoreCertificateErrors $IgnoreCertificateErrors
    )

    foreach ($Library in $Libraries) {

        if ($null -eq $Library) {
            continue
        }

        $LibraryName = $null

        if (Test-Property $Library "name") {
            $LibraryName = [string]$Library.name
        }
        elseif (
            (Test-Property $Library "contentLibraryRef") -and
            $null -ne $Library.contentLibraryRef -and
            (Test-Property $Library.contentLibraryRef "name")
        ) {
            $LibraryName =
                [string]$Library.contentLibraryRef.name
        }
        elseif (
            (Test-Property $Library "library") -and
            $null -ne $Library.library -and
            (Test-Property $Library.library "name")
        ) {
            $LibraryName =
                [string]$Library.library.name
        }

        if ([string]::IsNullOrWhiteSpace($LibraryName)) {
            continue
        }

        if ($LibraryName -ne $Name) {
            continue
        }

        if (Test-Property $Library "libraryType") {

            $LibraryType = [string]$Library.libraryType

            if (
                -not [string]::IsNullOrWhiteSpace($LibraryType) -and
                $LibraryType -ne "PROVIDER"
            ) {
                continue
            }
        }

        return $Library
    }

    return $null
}



function Get-VcfaStorageClasses {

    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [bool]$IgnoreCertificateErrors = $false
    )

    $SystemHeaders =
        New-VcfaSystemHeaders `
            -Headers $Headers

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/v1/storageClasses" `
        -Headers $SystemHeaders `
        -PageSize 128 `
        -IgnoreCertificateErrors $IgnoreCertificateErrors
}


function Resolve-VcfaContentLibraryStorageClasses {

    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        $Library,

        [bool]$IgnoreCertificateErrors = $false
    )

    $LibraryName = [string](
        Get-PropertyValue `
            -Object $Library `
            -Names @("name") `
            -Default "<unnamed>"
    )

    $ConfiguredStorageClasses =
        Get-PropertyValue `
            -Object $Library `
            -Names @("storage_classes") `
            -Default $null

    if (
        $null -eq $ConfiguredStorageClasses -or
        @($ConfiguredStorageClasses).Count -eq 0
    ) {
        throw (
            "Provider content library '$LibraryName' requires " +
            "storage_classes. Specify at least one region/storage class."
        )
    }

    $Available = @(
        Get-VcfaStorageClasses `
            -Server $Server `
            -Headers $Headers `
            -IgnoreCertificateErrors $IgnoreCertificateErrors
    )

    if ($Available.Count -eq 0) {
        throw "VCFA returned no available storage classes."
    }

    $Resolved = @()

    foreach ($Requested in @($ConfiguredStorageClasses)) {

        $RequestedName = $null
        $RequestedRegion = $null

        if ($Requested -is [string]) {
            $RequestedName = [string]$Requested
        }
        else {

            $RequestedName = [string](
                Get-PropertyValue `
                    -Object $Requested `
                    -Names @("name") `
                    -Default ""
            )

            $RequestedRegion = [string](
                Get-PropertyValue `
                    -Object $Requested `
                    -Names @("region") `
                    -Default ""
            )
        }

        if ([string]::IsNullOrWhiteSpace($RequestedName)) {
            throw (
                "provider_content_libraries[].storage_classes[].name " +
                "is required."
            )
        }

        $Matches = @()

        foreach ($StorageClass in $Available) {

            if ($null -eq $StorageClass) {
                continue
            }

            $StorageName = [string](
                Get-PropertyValue `
                    -Object $StorageClass `
                    -Names @("name") `
                    -Default ""
            )

            $KubernetesName = [string](
                Get-PropertyValue `
                    -Object $StorageClass `
                    -Names @("kubernetesCompliantName") `
                    -Default ""
            )

            if (
                $StorageName -ne $RequestedName -and
                $KubernetesName -ne $RequestedName
            ) {
                continue
            }

            $RegionRef =
                Get-VcfaObjectReference `
                    -Object $StorageClass `
                    -PropertyNames @(
                        "region",
                        "regionRef"
                    )

            $RegionName = ""

            if ($RegionRef) {
                $RegionName = [string](
                    Get-PropertyValue `
                        -Object $RegionRef `
                        -Names @("Name","name") `
                        -Default ""
                )
            }

            if (
                -not [string]::IsNullOrWhiteSpace($RequestedRegion) -and
                $RegionName -ne $RequestedRegion
            ) {
                continue
            }

            $Matches += $StorageClass
        }

        if ($Matches.Count -eq 0) {

            $RegionText = ""

            if (
                -not [string]::IsNullOrWhiteSpace(
                    $RequestedRegion
                )
            ) {
                $RegionText =
                    " in region '$RequestedRegion'"
            }

            throw (
                "Storage class '$RequestedName'$RegionText " +
                "was not found."
            )
        }

        if (
            $Matches.Count -gt 1 -and
            [string]::IsNullOrWhiteSpace($RequestedRegion)
        ) {

            $Regions = @(
                foreach ($CandidateMatch in $Matches) {

                    $CandidateRef =
                        Get-VcfaObjectReference `
                            -Object $CandidateMatch `
                            -PropertyNames @(
                                "region",
                                "regionRef"
                            )

                    if ($CandidateRef) {

                        $CandidateRegionName = [string](
                            Get-PropertyValue `
                                -Object $CandidateRef `
                                -Names @("Name","name") `
                                -Default ""
                        )

                        if (
                            -not [string]::IsNullOrWhiteSpace(
                                $CandidateRegionName
                            )
                        ) {
                            $CandidateRegionName
                        }
                    }
                }
            ) | Sort-Object -Unique

            throw (
                "Storage class '$RequestedName' exists in multiple " +
                "regions ($($Regions -join ', ')). Specify region."
            )
        }

        $Match =
            $Matches |
            Select-Object -First 1

        $StorageId =
            Get-VcfaObjectId `
                -Object $Match

        if (-not $StorageId) {
            throw (
                "Unable to determine ID for storage class " +
                "'$RequestedName'."
            )
        }

        $RegionRef =
            Get-VcfaObjectReference `
                -Object $Match `
                -PropertyNames @(
                    "region",
                    "regionRef"
                )

        if (-not $RegionRef) {
            throw (
                "Storage class '$RequestedName' does not contain " +
                "a valid region reference."
            )
        }

        $RegionId = [string](
            Get-PropertyValue `
                -Object $RegionRef `
                -Names @("Id","id") `
                -Default ""
        )

        $RegionName = [string](
            Get-PropertyValue `
                -Object $RegionRef `
                -Names @("Name","name") `
                -Default ""
        )

        if ([string]::IsNullOrWhiteSpace($RegionId)) {
            throw (
                "Storage class '$RequestedName' does not contain " +
                "a valid region ID."
            )
        }

        $ResolvedName = [string](
            Get-PropertyValue `
                -Object $Match `
                -Names @("name") `
                -Default $RequestedName
        )

        $ResolvedEntry = [ordered]@{
            name = $ResolvedName
            id   = $StorageId

            region = [ordered]@{
                name = $RegionName
                id   = $RegionId
            }
        }

        $Resolved += $ResolvedEntry

        Write-Info (
            "Content library storage class: " +
            "'$ResolvedName' in region '$RegionName'"
        )
    }

    return $Resolved
}

function New-VcfaProviderContentLibraryIfMissing {

    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        $Library,

        [bool]$IgnoreCertificateErrors = $false
    )

    $Name = [string](
        Get-PropertyValue `
            -Object $Library `
            -Names @("name") `
            -Default ""
    )

    if ([string]::IsNullOrWhiteSpace($Name)) {
        throw "provider_content_libraries[].name is required."
    }

    $Existing =
        Get-VcfaProviderContentLibrary `
            -Server $Server `
            -Headers $Headers `
            -Name $Name `
            -IgnoreCertificateErrors $IgnoreCertificateErrors

    if ($Existing) {

        Write-Skip (
            "Provider content library '$Name' already exists"
        )

        return $Existing
    }

    $SystemHeaders =
        New-VcfaSystemHeaders `
            -Headers $Headers

    #
    # Provider scope is determined by System auth context.
    # Only name is required by the API. Optional properties
    # are added when supplied by the JSON.
    #

    $Body = [ordered]@{
        name = $Name
    }

    if (Test-Property $Library "description") {
        $Body.description =
            [string]$Library.description
    }

    #
    # Keep libraryType explicit as well. VCFA determines
    # provider scope from System context.
    #

    $Body.libraryType = "PROVIDER"

    $Body.storageClasses = @(
        Resolve-VcfaContentLibraryStorageClasses `
            -Server $Server `
            -Headers $Headers `
            -Library $Library `
            -IgnoreCertificateErrors $IgnoreCertificateErrors
    )


    $RequestedSubscribed = $false

    if (Test-Property $Library "is_subscribed") {
        $RequestedSubscribed = [bool]$Library.is_subscribed
    }

    if (Test-Property $Library "is_shared") {
        $Body.isShared =
            [bool]$Library.is_shared
    }

    if (Test-Property $Library "auto_attach") {
        $Body.autoAttach =
            [bool]$Library.auto_attach
    }

    # Subscription rules:
    #
    # is_subscribed=true  + usable subscription URL:
    #     create subscribed content library
    #
    # is_subscribed=true  + no usable subscription URL:
    #     skip library completely
    #
    # is_subscribed=false:
    #     ignore subscription completely and create local library
    #
    $Body.isSubscribed = $false

    if ($RequestedSubscribed) {

        $SubscriptionUrl = ""
        $Subscription = $null

        if (Test-Property $Library "subscription") {

            $Subscription =
                $Library.subscription

            if (
                $null -ne $Subscription -and
                (Test-Property $Subscription "url")
            ) {
                $SubscriptionUrl =
                    [string]$Subscription.url
            }
        }

        if (
            [string]::IsNullOrWhiteSpace(
                $SubscriptionUrl
            )
        ) {

            Write-Skip (
                "Provider content library '$Name' has " +
                "is_subscribed=true but no usable subscription URL; " +
                "skipping library creation"
            )

            return $null
        }

        $SubscriptionBody = [ordered]@{
            subscriptionUrl =
                $SubscriptionUrl
        }

        if (
            $null -ne $Subscription -and
            (Test-Property $Subscription "authenticated")
        ) {
            $SubscriptionBody.authenticated =
                [bool]$Subscription.authenticated
        }

        if (
            $null -ne $Subscription -and
            (Test-Property $Subscription "password") -and
            $null -ne $Subscription.password
        ) {
            $SubscriptionBody.password =
                [string]$Subscription.password
        }

        if (
            $null -ne $Subscription -and
            (Test-Property $Subscription "need_local_copy")
        ) {
            $SubscriptionBody.needLocalCopy =
                [bool]$Subscription.need_local_copy
        }

        $Body.subscriptionConfig =
            $SubscriptionBody

        $Body.isSubscribed =
            $true
    }

    Write-Create (
        "Provider content library '$Name'"
    )

    try {

        Invoke-ApiRest `
            -Uri (
                "https://$Server/cloudapi/v1/" +
                "contentLibraries"
            ) `
            -Method POST `
            -Headers $SystemHeaders `
            -Body $Body `
            -IgnoreCertificateErrors $IgnoreCertificateErrors |
        Out-Null
    }
    catch {

        $Status =
            Get-HttpStatusCode $_

        Write-Host ""
        Write-Host (
            "Provider content library creation failed"
        ) -ForegroundColor Red

        Write-Host "Name:     $Name"
        Write-Host "HTTP:     $Status"
        Write-Host (
            "Endpoint: https://$Server/cloudapi/v1/contentLibraries"
        )

        Write-Host ""
        Write-Host "Payload:"

        $DebugBody =
            $Body |
            ConvertTo-Json -Depth 20 |
            ConvertFrom-Json

        if (
            Test-Property `
                $DebugBody `
                "subscriptionConfig"
        ) {

            if (
                $null -ne $DebugBody.subscriptionConfig -and
                (Test-Property $DebugBody.subscriptionConfig "password")
            ) {
                $DebugBody.subscriptionConfig.password =
                    "********"
            }
        }

        Write-Host (
            $DebugBody |
            ConvertTo-Json -Depth 20
        )

        Write-Host ""
        Write-Host "Response:"
        Write-Host (
            Get-RestErrorDetail $_
        )

        throw
    }

    #
    # POST returns 202 with a task URL in the Location header.
    # A successfully created subscribed library may initially
    # report NOT_READY while VCFA prepares/synchronises it.
    # Treat "resource exists" as successful creation.
    #

    for ($i = 0; $i -lt 60; $i++) {

        $Created =
            Get-VcfaProviderContentLibrary `
                -Server $Server `
                -Headers $Headers `
                -Name $Name `
                -IgnoreCertificateErrors $IgnoreCertificateErrors

        if ($Created) {

            $Status = ""

            if (Test-Property $Created "status") {
                $Status =
                    ([string]$Created.status).ToUpper()
            }

            if ($Status -eq "FAILED") {

                throw (
                    "Provider content library '$Name' " +
                    "was created but entered FAILED state."
                )
            }

            if ($Status -eq "NOT_READY") {

                Write-Host (
                    "[CREATED] Provider content library '$Name' " +
                    "(status=NOT_READY; VCFA is still preparing/syncing it)"
                ) -ForegroundColor Yellow

                return $Created
            }

            if ($Status -eq "PARTIALLY_READY") {

                Write-Host (
                    "[CREATED] Provider content library '$Name' " +
                    "(status=PARTIALLY_READY)"
                ) -ForegroundColor Yellow

                return $Created
            }

            if ($Status -eq "READY") {

                Write-Host (
                    "[CREATED] Provider content library '$Name' " +
                    "(status=READY)"
                ) -ForegroundColor Green

                return $Created
            }

            if ([string]::IsNullOrWhiteSpace($Status)) {

                Write-Host (
                    "[CREATED] Provider content library '$Name' " +
                    "(status not yet reported)"
                ) -ForegroundColor Green

                return $Created
            }

            Write-Warn (
                "Provider content library '$Name' exists with " +
                "status '$Status'."
            )

            return $Created
        }

        Write-Info (
            "Waiting for provider content library '$Name' " +
            "to appear (attempt $($i + 1)/60)"
        )

        Start-Sleep -Seconds 2
    }

    throw (
        "Content library create request was accepted, but " +
        "provider content library '$Name' did not appear in " +
        "VCFA inventory within 120 seconds."
    )
}

function Get-VcfaTenants {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[bool]$IgnoreCertificateErrors=$false)
    return Get-VcfaPagedValues -Server $Server -Path "/cloudapi/1.0.0/orgs" -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors
}
function Get-VcfaTenant {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[string]$Name,[bool]$IgnoreCertificateErrors=$false)
    return Get-VcfaTenants -Server $Server -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors | Where-Object {$_.name -eq $Name} | Select-Object -First 1
}
function Wait-VcfaTenantReady {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[string]$Name,[bool]$IgnoreCertificateErrors=$false)
    for ($i=0;$i -lt 60;$i++) {
        $Tenant=Get-VcfaTenant -Server $Server -Headers $Headers -Name $Name -IgnoreCertificateErrors $IgnoreCertificateErrors
        if ($Tenant) {
            if (-not (Test-Property $Tenant "creationStatus") -or $Tenant.creationStatus -eq "READY") { return $Tenant }
            if ($Tenant.creationStatus -in @("ERROR","FAILED_CREATION","CONFLICT")) { throw "Tenant '$Name' creation failed: $($Tenant.creationStatus)" }
        }
        Start-Sleep -Seconds 2
    }
    throw "Timed out waiting for tenant '$Name'."
}
function Get-OrCreateVcfaTenant {
    param([string]$Server,[System.Collections.IDictionary]$Headers,$Tenant,[bool]$IgnoreCertificateErrors=$false)
    $Existing=Get-VcfaTenant -Server $Server -Headers $Headers -Name $Tenant.name -IgnoreCertificateErrors $IgnoreCertificateErrors
    if ($Existing) { Write-Skip "VCFA tenant '$($Tenant.name)' already exists"; return $Existing }
    $DisplayName=$Tenant.name; if (Test-Property $Tenant "display_name") { $DisplayName=$Tenant.display_name }
    $Body=[ordered]@{name=$Tenant.name;displayName=$DisplayName}
    if (Test-Property $Tenant "description") { $Body.description=$Tenant.description }
    if (Test-Property $Tenant "enabled") { $Body.isEnabled=[bool]$Tenant.enabled }
    if (Test-Property $Tenant "isClassicTenant") { $Body.isClassicTenant=[bool]$Tenant.isClassicTenant }
    if (Test-Property $Tenant "isProviderConsumptionOrg") { $Body.isProviderConsumptionOrg=[bool]$Tenant.isProviderConsumptionOrg }
    Write-Create "VCFA tenant '$($Tenant.name)'"
    Invoke-ApiRest -Uri "https://$Server/cloudapi/1.0.0/orgs" -Method POST -Headers $Headers -Body $Body -IgnoreCertificateErrors $IgnoreCertificateErrors | Out-Null
    return Wait-VcfaTenantReady -Server $Server -Headers $Headers -Name $Tenant.name -IgnoreCertificateErrors $IgnoreCertificateErrors
}

function Get-VcfaRegions { param([string]$Server,[System.Collections.IDictionary]$Headers,[bool]$IgnoreCertificateErrors=$false) return Get-VcfaPagedValues -Server $Server -Path "/cloudapi/v1/regions" -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors }
function Get-VcfaRegion {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[string]$Name,[bool]$IgnoreCertificateErrors=$false)
    $Region=Get-VcfaRegions -Server $Server -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors | Where-Object {$_.name -eq $Name} | Select-Object -First 1
    if (-not $Region) { throw "VCFA region '$Name' not found." }
    return $Region
}

function Get-VcfaZones {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[string]$RegionId,[bool]$IgnoreCertificateErrors=$false)
    $Zones=Get-VcfaPagedValues -Server $Server -Path "/cloudapi/v1/zones" -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors
    $Results=@()
    foreach ($Zone in @($Zones)) {
        $RegionRef=Get-VcfaObjectReference -Object $Zone -PropertyNames @("region","regionRef")
        if ($RegionRef -and $RegionRef.Id -eq $RegionId) { $Results += $Zone }
    }
    return $Results
}
function Get-VcfaSupervisors {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[string]$RegionId,[bool]$IgnoreCertificateErrors=$false)
    $Supervisors=Get-VcfaPagedValues -Server $Server -Path "/cloudapi/v1/supervisors" -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors
    $Results=@()
    foreach ($Supervisor in @($Supervisors)) {
        $RegionRef=Get-VcfaObjectReference -Object $Supervisor -PropertyNames @("region","regionRef")
        if ($RegionRef -and $RegionRef.Id -eq $RegionId) { $Results += $Supervisor }
    }
    return $Results
}

function Get-VcfaVirtualDatacenters { param([string]$Server,[System.Collections.IDictionary]$Headers,[bool]$IgnoreCertificateErrors=$false) return Get-VcfaPagedValues -Server $Server -Path "/cloudapi/v1/virtualDatacenters" -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors }
function Get-VcfaRegionQuota {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[string]$OrgId,[string]$RegionId,[bool]$IgnoreCertificateErrors=$false)
    $VirtualDatacenters=Get-VcfaVirtualDatacenters -Server $Server -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors
    foreach ($Vdc in @($VirtualDatacenters)) {
        $OrgRef=Get-VcfaObjectReference -Object $Vdc -PropertyNames @("org","orgRef")
        $RegionRef=Get-VcfaObjectReference -Object $Vdc -PropertyNames @("region","regionRef")
        if ($OrgRef -and $RegionRef -and $OrgRef.Id -eq $OrgId -and $RegionRef.Id -eq $RegionId) { return $Vdc }
    }
    return $null
}

function New-VcfaRegionQuotaIfMissing {
    param([string]$Server,[System.Collections.IDictionary]$Headers,$Tenant,$QuotaConfig,[bool]$IgnoreCertificateErrors=$false)
    $Region=Get-VcfaRegion -Server $Server -Headers $Headers -Name $QuotaConfig.region -IgnoreCertificateErrors $IgnoreCertificateErrors
    $TenantId=Get-VcfaObjectId -Object $Tenant; $RegionId=Get-VcfaObjectId -Object $Region
    if (-not $TenantId) { throw "Unable to determine tenant ID for '$($Tenant.name)'." }
    if (-not $RegionId) { throw "Unable to determine region ID for '$($Region.name)'." }
    $Existing=Get-VcfaRegionQuota -Server $Server -Headers $Headers -OrgId $TenantId -RegionId $RegionId -IgnoreCertificateErrors $IgnoreCertificateErrors
    if ($Existing) { Write-Skip "Regional quota for '$($Tenant.name)' / '$($Region.name)' already exists"; return $Existing }
    $FullAllocation=$false; if (Test-Property $QuotaConfig "share_all") { $FullAllocation=[bool]$QuotaConfig.share_all }
    $Body=[ordered]@{name="$($Tenant.name)-$($Region.name)";org=@{name=$Tenant.name;id=$TenantId};region=@{name=$Region.name;id=$RegionId};isFullAllocation=$FullAllocation}
    if (-not $FullAllocation) {
        $AvailableSupervisors=@(Get-VcfaSupervisors -Server $Server -Headers $Headers -RegionId $RegionId -IgnoreCertificateErrors $IgnoreCertificateErrors)
        $SelectedSupervisors=@()
        if ((Test-Property $QuotaConfig "all_supervisors") -and [bool]$QuotaConfig.all_supervisors) { $SelectedSupervisors=@($AvailableSupervisors) }
        elseif (Test-Property $QuotaConfig "supervisors") {
            foreach ($SupervisorName in @($QuotaConfig.supervisors)) {
                $Supervisor=$AvailableSupervisors | Where-Object {$_.name -eq $SupervisorName} | Select-Object -First 1
                if (-not $Supervisor) { throw "Supervisor '$SupervisorName' not found in region '$($Region.name)'." }
                $SelectedSupervisors += $Supervisor
            }
        }
        $Body.supervisors=@($SelectedSupervisors | ForEach-Object {@{name=$_.name;id=(Get-VcfaObjectId -Object $_)}})
    }
    if (-not $FullAllocation -and (Test-Property $QuotaConfig "capacity")) {
        $Capacity=$QuotaConfig.capacity
        $AvailableZones=@(Get-VcfaZones -Server $Server -Headers $Headers -RegionId $RegionId -IgnoreCertificateErrors $IgnoreCertificateErrors)
        $ZoneConfigs=if ((Test-Property $Capacity "all_zones") -and [bool]$Capacity.all_zones) {
            @($AvailableZones | ForEach-Object {[PSCustomObject]@{zone=$_.name;cpu_limit_GHz=0;memory_limit_GB=0;cpu_reservation_GHz=0;memory_reservation_GB=0}})
        } else { @($Capacity.zones) }
        $Body.zoneResourceAllocation=@()
        foreach ($ZoneConfig in $ZoneConfigs) {
            $Zone=$null
            foreach ($AvailableZone in $AvailableZones) {
                $AvailableZoneName = $null

                if (Test-Property $AvailableZone "name") {
                    $AvailableZoneName = [string]$AvailableZone.name
                }

                if (-not $AvailableZoneName -and (Test-Property $AvailableZone "zone")) {
                    $NestedZone = $AvailableZone.zone

                    if ($null -ne $NestedZone -and (Test-Property $NestedZone "name")) {
                        $AvailableZoneName = [string]$NestedZone.name
                    }
                }

                $ConfiguredZoneName = $null
                if (Test-Property $ZoneConfig "zone") {
                    $ConfiguredZoneName = [string]$ZoneConfig.zone
                }

                if ($AvailableZoneName -eq $ConfiguredZoneName) {
                    $Zone = $AvailableZone
                    break
                }
            }

            $ConfiguredZoneName = $null
            if (Test-Property $ZoneConfig "zone") {
                $ConfiguredZoneName = [string]$ZoneConfig.zone
            }

            if (-not $Zone) {
                throw "Zone '$ConfiguredZoneName' not found in region '$($Region.name)'."
            }

            $ZoneName = $null
            if (Test-Property $Zone "name") {
                $ZoneName = [string]$Zone.name
            }

            $ZoneId = Get-VcfaObjectId -Object $Zone

            if (Test-Property $Zone "zone") {
                $NestedZone = $Zone.zone

                if ($null -ne $NestedZone) {
                    if (Test-Property $NestedZone "name") {
                        $ZoneName = [string]$NestedZone.name
                    }

                    $NestedZoneId = Get-VcfaObjectId -Object $NestedZone
                    if ($NestedZoneId) {
                        $ZoneId = $NestedZoneId
                    }
                }
            }

            if (-not $ZoneId) {
                throw "Unable to determine ID for zone '$ConfiguredZoneName'."
            }
            $Body.zoneResourceAllocation += [ordered]@{zone=@{name=$ZoneName;id=$ZoneId};resourceAllocation=@{cpuLimitMHz=[int64]([double]$ZoneConfig.cpu_limit_GHz*1000);cpuReservationMHz=[int64]([double]$ZoneConfig.cpu_reservation_GHz*1000);memoryLimitMiB=[int64]([double]$ZoneConfig.memory_limit_GB*1024);memoryReservationMiB=[int64]([double]$ZoneConfig.memory_reservation_GB*1024)}}
        }
    }
    Write-Create "VCFA regional quota '$($Body.name)'"
    Invoke-ApiRest -Uri "https://$Server/cloudapi/v1/virtualDatacenters" -Method POST -Headers $Headers -Body $Body -IgnoreCertificateErrors $IgnoreCertificateErrors | Out-Null
    for ($i=0;$i -lt 60;$i++) {
        $Quota=Get-VcfaRegionQuota -Server $Server -Headers $Headers -OrgId $TenantId -RegionId $RegionId -IgnoreCertificateErrors $IgnoreCertificateErrors
        if ($Quota) { if (-not (Test-Property $Quota "status") -or $Quota.status -eq "READY") { return $Quota }; if ($Quota.status -eq "FAILED") { throw "Region quota '$($Body.name)' failed." } }
        Start-Sleep -Seconds 2
    }
    throw "Timed out waiting for regional quota '$($Body.name)'."
}

function Get-VcfaVdcRegionReference {
    param([Parameter(Mandatory)]$Vdc)
    $RegionRef=Get-VcfaObjectReference -Object $Vdc -PropertyNames @("region","regionRef")
    if (-not $RegionRef) { $Name=if (Test-Property $Vdc "name") {[string]$Vdc.name} else {"<unknown>"}; throw "VDC '$Name' does not contain a valid region reference." }
    return $RegionRef
}

function Get-VcfaRegionVmClasses {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[string]$RegionId,[bool]$IgnoreCertificateErrors=$false)
    $Values=Get-VcfaPagedValues -Server $Server -Path "/cloudapi/v1/virtualMachineClasses" -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors
    $Results=@(); foreach ($Item in @($Values)) { $RegionRef=Get-VcfaObjectReference -Object $Item -PropertyNames @("region","regionRef"); if ($RegionRef -and $RegionRef.Id -eq $RegionId) { $Results += $Item } }
    return $Results
}
function Set-VcfaVdcVmClasses {
    param([string]$Server,[System.Collections.IDictionary]$Headers,$Vdc,$Config,[bool]$IgnoreCertificateErrors=$false)
    $IsFullAllocation=$false; if (Test-Property $Vdc "isFullAllocation") { $IsFullAllocation=[bool]$Vdc.isFullAllocation }
    if ($IsFullAllocation) { Write-Skip "VM classes inherited because regional quota has full allocation"; return }
    $RegionRef=Get-VcfaVdcRegionReference -Vdc $Vdc; $VdcId=Get-VcfaObjectId -Object $Vdc; if (-not $VdcId) { throw "Unable to determine VDC ID." }
    $Available=@(Get-VcfaRegionVmClasses -Server $Server -Headers $Headers -RegionId $RegionRef.Id -IgnoreCertificateErrors $IgnoreCertificateErrors)
    $Selected=@()
    if ((Test-Property $Config "all_classes") -and [bool]$Config.all_classes) { $Selected=@($Available) }
    else { foreach ($Name in @($Config.classes)) { $Match=$Available | Where-Object {$_.name -eq $Name} | Select-Object -First 1; if (-not $Match) { throw "VM class '$Name' not found in region '$($RegionRef.Name)'." }; $Selected += $Match } }
    $Body=@{values=@($Selected | ForEach-Object {@{name=$_.name;id=(Get-VcfaObjectId -Object $_)}})}
    Write-Info "Assigning $($Body.values.Count) VM classes to '$($Vdc.name)'"
    Invoke-ApiRest -Uri "https://$Server/cloudapi/v1/virtualDatacenters/$VdcId/virtualMachineClasses" -Method PUT -Headers $Headers -Body $Body -IgnoreCertificateErrors $IgnoreCertificateErrors | Out-Null
}

function Get-VcfaRegionStoragePolicies {
    param([string]$Server,[System.Collections.IDictionary]$Headers,[string]$RegionId,[bool]$IgnoreCertificateErrors=$false)
    $Values=Get-VcfaPagedValues -Server $Server -Path "/cloudapi/v1/regionStoragePolicies" -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors
    $Results=@(); foreach ($Item in @($Values)) { $RegionRef=Get-VcfaObjectReference -Object $Item -PropertyNames @("region","regionRef"); if ($RegionRef -and $RegionRef.Id -eq $RegionId) { $Results += $Item } }
    return $Results
}
function Set-VcfaVdcStorageClasses {
    param([string]$Server,[System.Collections.IDictionary]$Headers,$Vdc,$Config,[bool]$IgnoreCertificateErrors=$false)
    $VdcId=Get-VcfaObjectId -Object $Vdc; if (-not $VdcId) { throw "VDC does not contain a usable id." }
    $VdcName=if (Test-Property $Vdc "name") {[string]$Vdc.name} else {"<unknown>"}
    $RegionRef=Get-VcfaVdcRegionReference -Vdc $Vdc
    $Available=@(Get-VcfaRegionStoragePolicies -Server $Server -Headers $Headers -RegionId $RegionRef.Id -IgnoreCertificateErrors $IgnoreCertificateErrors)
    if ($Available.Count -eq 0) { throw "No storage policies found in region '$($RegionRef.Name)'." }
    $Selected=@()
    if ((Test-Property $Config "all_classes") -and [bool]$Config.all_classes) { $Selected=@($Available); Write-Info "Using all $($Selected.Count) storage classes for '$VdcName'" }
    else {
        foreach ($Name in @($Config.classes)) {
            $Match=$null
            foreach ($Item in $Available) {
                $ItemName=if (Test-Property $Item "name") {[string]$Item.name} else {$null}
                $KubernetesName=if (Test-Property $Item "kubernetesCompliantName") {[string]$Item.kubernetesCompliantName} else {$null}
                if ($ItemName -eq $Name -or $KubernetesName -eq $Name) { $Match=$Item; break }
            }
            if (-not $Match) { throw "Storage class '$Name' not found in region '$($RegionRef.Name)'." }
            $Selected += $Match
        }
    }
    if ($Selected.Count -eq 0) { Write-Skip "No storage classes selected for '$VdcName'"; return }
    $Values=@()
    foreach ($StoragePolicy in $Selected) {
        $StoragePolicyId=Get-VcfaObjectId -Object $StoragePolicy; if (-not $StoragePolicyId) { throw "Storage policy '$($StoragePolicy.name)' does not contain a usable id." }
        $StoragePolicyName=if (Test-Property $StoragePolicy "name") {[string]$StoragePolicy.name} else {$StoragePolicyId}
        $Values += [ordered]@{virtualDatacenter=[ordered]@{name=$VdcName;id=$VdcId};regionStoragePolicy=[ordered]@{name=$StoragePolicyName;id=$StoragePolicyId};storageLimitMiB=0}
    }
    $Body=[ordered]@{values=$Values}
    $Uri="https://$Server/cloudapi/v1/virtualDatacenters/$VdcId/virtualDatacenterStoragePolicies"
    Write-Info "Assigning $($Values.Count) storage classes to VDC '$VdcName'"
    try { Invoke-ApiRest -Uri $Uri -Method PUT -Headers $Headers -Body $Body -IgnoreCertificateErrors $IgnoreCertificateErrors | Out-Null; Write-Host "[UPDATED] Storage classes for '$VdcName'" -ForegroundColor Green }
    catch { $Status=Get-HttpStatusCode $_; Write-Host ""; Write-Host "VCFA storage class assignment failed" -ForegroundColor Red; Write-Host "VDC:      $VdcName"; Write-Host "VDC ID:   $VdcId"; Write-Host "HTTP:     $Status"; Write-Host "Endpoint: $Uri"; Write-Host ""; Write-Host "Payload:"; Write-Host ($Body | ConvertTo-Json -Depth 20); Write-Host ""; Write-Host "Response:"; Write-Host (Get-RestErrorDetail $_); throw }
}

function Get-VcfaRegionInfraPolicies {
    param (
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        [string]$RegionId,

        [bool]$IgnoreCertificateErrors = $false
    )

    Write-Info "Getting infrastructure policies compatible with region '$RegionId'"

    # Do not call /cloudapi/v1/regions/{regionUrn}/infraPolicies here.
    # Some VCF Automation 9.1 builds can return an internal pagination NPE.
    # Query the global collection and filter compatibleRegionZones locally.
    $AllPolicies = @(
        Get-VcfaPagedValues `
            -Server $Server `
            -Path "/cloudapi/v1/infraPolicies" `
            -Headers $Headers `
            -IgnoreCertificateErrors $IgnoreCertificateErrors
    )

    if ($AllPolicies.Count -eq 0) {
        Write-Warn "No VCFA infrastructure policies were returned."
        return @()
    }

    $Results = @()

    foreach ($Policy in $AllPolicies) {

        if (-not (Test-Property $Policy "compatibleRegionZones")) {
            continue
        }

        if ($null -eq $Policy.compatibleRegionZones) {
            continue
        }

        foreach ($Compatibility in @($Policy.compatibleRegionZones)) {

            if ($null -eq $Compatibility) {
                continue
            }

            # Different API responses can expose either region or regionRef.
            $CompatibleRegionRef = Get-VcfaObjectReference `
                -Object $Compatibility `
                -PropertyNames @("region", "regionRef")

            if (
                $CompatibleRegionRef -and
                $CompatibleRegionRef.Id -eq $RegionId
            ) {
                $Results += $Policy
                break
            }
        }
    }

    Write-Info "Found $($Results.Count) infrastructure policies compatible with region '$RegionId'"
    return $Results
}

function Set-VcfaVdcInfraPolicies {
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

    $VdcId = Get-VcfaObjectId -Object $Vdc

    if (-not $VdcId) {
        throw "Unable to determine VDC ID."
    }

    $VdcName = $VdcId
    if (Test-Property $Vdc "name") {
        $VdcName = [string]$Vdc.name
    }

    $RegionRef = Get-VcfaVdcRegionReference -Vdc $Vdc

    Write-Info (
        "Resolving infrastructure policies for VDC '$VdcName' " +
        "in region '$($RegionRef.Name)'"
    )

    $Available = @(
        Get-VcfaRegionInfraPolicies `
            -Server $Server `
            -Headers $Headers `
            -RegionId $RegionRef.Id `
            -IgnoreCertificateErrors $IgnoreCertificateErrors
    )

    if ($Available.Count -eq 0) {
        Write-Warn (
            "No infrastructure policies compatible with region " +
            "'$($RegionRef.Name)' were found."
        )
        return
    }

    $Selected = @()

    if (
        (Test-Property $Config "all_policies") -and
        [bool]$Config.all_policies
    ) {
        $Selected = @($Available)
        Write-Info "Selecting all $($Selected.Count) compatible infrastructure policies"
    }
    else {
        $Names = @()

        if (Test-Property $Config "policies") {
            $Names = @($Config.policies)
        }
        elseif (Test-Property $Config "polices") {
            # Backward compatibility with the original typo.
            $Names = @($Config.polices)
        }

        foreach ($Name in $Names) {
            $Match = $Available |
                Where-Object { $_.name -eq $Name } |
                Select-Object -First 1

            if (-not $Match) {
                throw (
                    "Infrastructure policy '$Name' is not compatible " +
                    "with region '$($RegionRef.Name)'."
                )
            }

            $Selected += $Match
        }
    }

    if ($Selected.Count -eq 0) {
        Write-Skip "No infrastructure policies selected for '$VdcName'"
        return
    }

    $Values = @()

    foreach ($Policy in $Selected) {
        $PolicyId = Get-VcfaObjectId -Object $Policy

        if (-not $PolicyId) {
            throw "Unable to determine ID for infrastructure policy '$($Policy.name)'."
        }

        Write-Info "Infrastructure policy: '$($Policy.name)' [$PolicyId]"

        $Values += [ordered]@{
            infraPolicy = [ordered]@{
                name = $Policy.name
                id   = $PolicyId
            }
            status = "READY"
        }
    }

    $Body = [ordered]@{
        values = $Values
    }

    $Uri = (
        "https://$Server/cloudapi/v1/" +
        "virtualDatacenters/$VdcId/infraPolicies"
    )

    Write-Info "Assigning $($Values.Count) infrastructure policies to '$VdcName'"

    try {
        Invoke-ApiRest `
            -Uri $Uri `
            -Method PUT `
            -Headers $Headers `
            -Body $Body `
            -IgnoreCertificateErrors $IgnoreCertificateErrors |
            Out-Null

        Write-Host "[UPDATED] Infrastructure policies for '$VdcName'" -ForegroundColor Green
    }
    catch {
        $Status = Get-HttpStatusCode $_

        Write-Host ""
        Write-Host "VCFA infrastructure policy assignment failed" -ForegroundColor Red
        Write-Host "VDC:      $VdcName"
        Write-Host "VDC ID:   $VdcId"
        Write-Host "Region:   $($RegionRef.Name)"
        Write-Host "HTTP:     $Status"
        Write-Host "Endpoint: $Uri"

        Write-Host ""
        Write-Host "Payload:"
        Write-Host ($Body | ConvertTo-Json -Depth 20)

        Write-Host ""
        Write-Host "Response:"
        Write-Host (Get-RestErrorDetail $_)

        throw
    }
}

function Get-VcfaRegionalNetworkingSettings {
    param(
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [bool]$IgnoreCertificateErrors=$false
    )
    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/v1/regionalNetworkingSettings" `
        -Headers $Headers `
        -PageSize 32 `
        -IgnoreCertificateErrors $IgnoreCertificateErrors
}

function Get-VcfaDistributedVlanConnections {
    param(
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [bool]$IgnoreCertificateErrors=$false
    )
    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/v1/distributedVlanConnections" `
        -Headers $Headers `
        -IgnoreCertificateErrors $IgnoreCertificateErrors
}

function Get-VcfaVnaClusters {
    param(
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        [bool]$IgnoreCertificateErrors=$false
    )
    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/v1/virtualNetworkApplianceClusters" `
        -Headers $Headers `
        -IgnoreCertificateErrors $IgnoreCertificateErrors
}

function Get-OrCreateVcfaRegionalNetworkingSetting {
    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        $Tenant,

        [Parameter(Mandatory)]
        $Region,

        [Parameter(Mandatory)]
        $ExternalConnection,

        [bool]$IgnoreCertificateErrors=$false
    )

    $TenantId = Get-VcfaObjectId -Object $Tenant
    $RegionId = Get-VcfaObjectId -Object $Region

    if (-not $TenantId) {
        throw "Unable to determine tenant ID for '$($Tenant.name)'."
    }

    if (-not $RegionId) {
        throw "Unable to determine region ID for '$($Region.name)'."
    }

    # Support both formats:
    #   "external_connection": "distributed-vlan-connection-wld-a"
    # and:
    #   "external_connection": {
    #       "distributed": true,
    #       "name": "distributed-vlan-connection-wld-a",
    #       "cluster": "vna-cluster-wld01-a"
    #   }

    $ConnectionName = $null
    $ClusterName = $null
    $IsDistributed = $true

    if ($ExternalConnection -is [string]) {
        $ConnectionName = [string]$ExternalConnection
    }
    else {
        if (-not (Test-Property $ExternalConnection "name")) {
            throw "external_connection.name is required."
        }

        $ConnectionName = [string]$ExternalConnection.name

        if (Test-Property $ExternalConnection "cluster") {
            $ClusterName = [string]$ExternalConnection.cluster
        }

        if (Test-Property $ExternalConnection "distributed") {
            $IsDistributed = [bool]$ExternalConnection.distributed
        }
    }

    if ([string]::IsNullOrWhiteSpace($ConnectionName)) {
        throw "external_connection must specify a Distributed VLAN Connection name."
    }

    if (-not $IsDistributed) {
        throw "This script currently supports Distributed VLAN external connections only."
    }

    foreach ($Setting in @(Get-VcfaRegionalNetworkingSettings -Server $Server -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors)) {
        $OrgRef = Get-VcfaObjectReference -Object $Setting -PropertyNames @("orgRef","org")
        $SettingRegionRef = Get-VcfaObjectReference -Object $Setting -PropertyNames @("regionRef","region")

        if (
            $OrgRef -and
            $SettingRegionRef -and
            $OrgRef.Id -eq $TenantId -and
            $SettingRegionRef.Id -eq $RegionId
        ) {
            Write-Skip "Regional networking setting already exists for '$($Tenant.name)' / '$($Region.name)'"
            return $Setting
        }
    }

    Write-Info "Resolving Distributed VLAN Connection '$ConnectionName'"

    $Connection = $null
    foreach ($Candidate in @(Get-VcfaDistributedVlanConnections -Server $Server -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors)) {
        if (-not (Test-Property $Candidate "name")) { continue }
        if ([string]$Candidate.name -ne $ConnectionName) { continue }

        $CandidateRegionRef = Get-VcfaObjectReference -Object $Candidate -PropertyNames @("regionRef","region")
        if ($CandidateRegionRef -and $CandidateRegionRef.Id -ne $RegionId) { continue }

        $Connection = $Candidate
        break
    }

    if (-not $Connection) {
        throw "Distributed VLAN Connection '$ConnectionName' not found in region '$($Region.name)'."
    }

    $ConnectionId = Get-VcfaObjectId -Object $Connection
    if (-not $ConnectionId) {
        throw "Unable to determine ID for Distributed VLAN Connection '$ConnectionName'."
    }

    Write-Info "Using Distributed VLAN Connection '$ConnectionName' [$ConnectionId]"

    $RegionVnaClusters = @()
    foreach ($Candidate in @(Get-VcfaVnaClusters -Server $Server -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors)) {
        $CandidateRegionRef = Get-VcfaObjectReference -Object $Candidate -PropertyNames @("regionRef","region")

        if (
            -not $CandidateRegionRef -or
            $CandidateRegionRef.Id -eq $RegionId
        ) {
            $RegionVnaClusters += $Candidate
        }
    }

    $VnaCluster = $null

    if (-not [string]::IsNullOrWhiteSpace($ClusterName)) {
        Write-Info "Resolving VNA Cluster '$ClusterName'"

        $VnaCluster = $RegionVnaClusters |
            Where-Object {
                (Test-Property $_ "name") -and
                [string]$_.name -eq $ClusterName
            } |
            Select-Object -First 1

        if (-not $VnaCluster) {
            throw "VNA Cluster '$ClusterName' not found in region '$($Region.name)'."
        }
    }
    else {
        if ($RegionVnaClusters.Count -eq 1) {
            $VnaCluster = $RegionVnaClusters[0]
            $ClusterName = [string]$VnaCluster.name

            Write-Info (
                "Automatically selected the only compatible VNA Cluster " +
                "'$ClusterName' in region '$($Region.name)'"
            )
        }
        elseif ($RegionVnaClusters.Count -eq 0) {
            throw (
                "No VNA Cluster was found in region '$($Region.name)'. " +
                "Use the structured external_connection format and specify cluster."
            )
        }
        else {
            $Names = @(
                $RegionVnaClusters |
                    ForEach-Object {
                        if (Test-Property $_ "name") { [string]$_.name }
                    }
            ) -join ", "

            throw (
                "Multiple VNA Clusters are available in region '$($Region.name)': $Names. " +
                "Use the structured external_connection format and specify cluster."
            )
        }
    }

    $VnaClusterId = Get-VcfaObjectId -Object $VnaCluster
    if (-not $VnaClusterId) {
        throw "Unable to determine ID for VNA Cluster '$ClusterName'."
    }

    Write-Info "Using VNA Cluster '$ClusterName' [$VnaClusterId]"

    $Body = [ordered]@{
        name = "$($Tenant.name)-$($Region.name)"
        orgRef = [ordered]@{
            name = $Tenant.name
            id   = $TenantId
        }
        regionRef = [ordered]@{
            name = $Region.name
            id   = $RegionId
        }
        distributedVlanConnectionRef = [ordered]@{
            name = $ConnectionName
            id   = $ConnectionId
        }
        virtualNetworkApplianceClusterRef = [ordered]@{
            name = $ClusterName
            id   = $VnaClusterId
        }
    }

    Write-Create "Regional networking setting '$($Body.name)'"
    Write-Info "Initial Distributed VLAN Connection: '$ConnectionName'"
    Write-Info "Initial VNA Cluster: '$ClusterName'"

    try {
        Invoke-ApiRest `
            -Uri "https://$Server/cloudapi/v1/regionalNetworkingSettings" `
            -Method POST `
            -Headers $Headers `
            -Body $Body `
            -IgnoreCertificateErrors $IgnoreCertificateErrors |
            Out-Null
    }
    catch {
        Write-Host ""
        Write-Host "Regional Networking Setting creation failed" -ForegroundColor Red
        Write-Host "Tenant:     $($Tenant.name)"
        Write-Host "Region:     $($Region.name)"
        Write-Host "Connection: $ConnectionName"
        Write-Host "Cluster:    $ClusterName"
        Write-Host ""
        Write-Host "Payload:"
        Write-Host ($Body | ConvertTo-Json -Depth 20)
        Write-Host ""
        Write-Host "Response:"
        Write-Host (Get-RestErrorDetail $_)
        throw
    }

    for ($i=0; $i -lt 60; $i++) {
        foreach ($Setting in @(Get-VcfaRegionalNetworkingSettings -Server $Server -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors)) {
            $OrgRef = Get-VcfaObjectReference -Object $Setting -PropertyNames @("orgRef","org")
            $SettingRegionRef = Get-VcfaObjectReference -Object $Setting -PropertyNames @("regionRef","region")

            if (
                $OrgRef -and
                $SettingRegionRef -and
                $OrgRef.Id -eq $TenantId -and
                $SettingRegionRef.Id -eq $RegionId
            ) {
                Write-Host "[CREATED] Regional networking setting '$($Setting.name)'" -ForegroundColor Green
                return $Setting
            }
        }

        Start-Sleep -Seconds 2
    }

    throw "Timed out waiting for Regional Networking Setting '$($Tenant.name)-$($Region.name)'."
}

function Set-VcfaExternalConnection {
    param(
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        $Tenant,
        $Region,
        $ExternalConnection,
        [bool]$IgnoreCertificateErrors=$false
    )

    $Setting = Get-OrCreateVcfaRegionalNetworkingSetting `
        -Server $Server `
        -Headers $Headers `
        -Tenant $Tenant `
        -Region $Region `
        -ExternalConnection $ExternalConnection `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    $DisplayConnectionName = if ($ExternalConnection -is [string]) { [string]$ExternalConnection } else { [string]$ExternalConnection.name }
    Write-Skip "External connection '$DisplayConnectionName' configured through Regional Networking Setting"
    return $Setting
}

$ResolvedConfig=(Resolve-Path $ConfigFile).Path
$ConfigDirectory=Split-Path -Parent $ResolvedConfig
Write-Info "Loading '$ResolvedConfig'"
$Config=Get-Content -Path $ResolvedConfig -Raw | ConvertFrom-Json

Write-Info "Using nested configuration schema: vcenter.* and vcfa.*"

if (-not (Test-Property $Config "vcenter")) { throw "vcenter section is required." }
if (-not $Config.vcenter.server) { throw "vcenter.server is required." }
if (-not $Config.vcenter.username) { throw "vcenter.username is required." }
if (-not $Config.vcenter.password_file) { throw "vcenter.password_file is required." }

$PasswordFile=Resolve-ConfigFilePath -Path $Config.vcenter.password_file -ConfigDirectory $ConfigDirectory
if (-not (Test-Path $PasswordFile)) { throw "vCenter password file not found: $PasswordFile" }
$Password=(Get-Content -Path $PasswordFile -Raw).Trim()
$SecurePassword=ConvertTo-SecureString -String $Password -AsPlainText -Force
$Credential=[PSCredential]::new($Config.vcenter.username,$SecurePassword)
$IgnoreCertErrors=$false
if (Test-Property $Config.vcenter "ignore_certificate_errors") { $IgnoreCertErrors=[bool]$Config.vcenter.ignore_certificate_errors }
if ($IgnoreCertErrors) { Set-PowerCLIConfiguration -InvalidCertificateAction Ignore -Confirm:$false | Out-Null }

$VcfaConfigured=((Test-Property $Config "vcfa") -and ($null -ne $Config.vcfa) -and -not [string]::IsNullOrWhiteSpace([string]$Config.vcfa.server))
$VcfaHeaders=$null; $VcfaIgnoreCertErrors=$false
if ($VcfaConfigured) {
    if (-not $Config.vcfa.api_token_file) { throw "vcfa.api_token_file is required." }
    if (Test-Property $Config.vcfa "ignore_certificate_errors") { $VcfaIgnoreCertErrors=[bool]$Config.vcfa.ignore_certificate_errors }
    $TokenFile=Resolve-ConfigFilePath -Path $Config.vcfa.api_token_file -ConfigDirectory $ConfigDirectory
    $ApiToken=Get-VcfaApiToken -TokenFile $TokenFile
    $AccessToken=Get-VcfaAccessToken -Server $Config.vcfa.server -ApiToken $ApiToken -IgnoreCertificateErrors $VcfaIgnoreCertErrors
    $VcfaHeaders=New-VcfaHeaders -AccessToken $AccessToken
    $ApiToken=$null
} else { Write-Skip "VCFA configuration not provided" }

$VIServer=$null; $RestHeaders=$null
function Get-VcfaContentLibraryItems {

    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [bool]$IgnoreCertificateErrors = $false
    )

    $SystemHeaders =
        New-VcfaSystemHeaders `
            -Headers $Headers

    return Get-VcfaPagedValues `
        -Server $Server `
        -Path "/cloudapi/v1/contentLibraryItems" `
        -Headers $SystemHeaders `
        -PageSize 64 `
        -IgnoreCertificateErrors $IgnoreCertificateErrors
}


function Get-VcfaContentLibraryItemByName {

    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        [string]$LibraryId,

        [Parameter(Mandatory)]
        [string]$Name,

        [bool]$IgnoreCertificateErrors = $false
    )

    foreach (
        $Item in @(
            Get-VcfaContentLibraryItems `
                -Server $Server `
                -Headers $Headers `
                -IgnoreCertificateErrors $IgnoreCertificateErrors
        )
    ) {

        if ($null -eq $Item) {
            continue
        }

        $ItemName = [string](
            Get-PropertyValue `
                -Object $Item `
                -Names @("name") `
                -Default ""
        )

        if ($ItemName -ne $Name) {
            continue
        }

        $LibraryRef =
            Get-VcfaObjectReference `
                -Object $Item `
                -PropertyNames @(
                    "contentLibrary",
                    "contentLibraryRef"
                )

        if (-not $LibraryRef) {
            continue
        }

        $ItemLibraryId = [string](
            Get-PropertyValue `
                -Object $LibraryRef `
                -Names @("Id","id") `
                -Default ""
        )

        if ($ItemLibraryId -eq $LibraryId) {
            return $Item
        }
    }

    return $null
}


function Get-VcfaContentLibraryItemFiles {

    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        [string]$ItemId,

        [bool]$IgnoreCertificateErrors = $false
    )

    $SystemHeaders =
        New-VcfaSystemHeaders `
            -Headers $Headers

    $EncodedItemId =
        [uri]::EscapeDataString($ItemId)

    $Result =
        Invoke-ApiRest `
            -Uri (
                "https://$Server/cloudapi/v1/" +
                "contentLibraryItems/$EncodedItemId/" +
                "files?page=1&pageSize=128"
            ) `
            -Method GET `
            -Headers $SystemHeaders `
            -IgnoreCertificateErrors $IgnoreCertificateErrors

    if (
        $Result -and
        (Test-Property $Result "values")
    ) {
        return @($Result.values)
    }

    if ($Result -is [System.Array]) {
        return @($Result)
    }

    return @()
}


function Wait-VcfaContentLibraryItemTransferUrl {

    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        [string]$ItemId,

        [Parameter(Mandatory)]
        [string]$LocalFileName,

        [int]$TimeoutSeconds = 120,

        [int]$PollIntervalSeconds = 2,

        [bool]$IgnoreCertificateErrors = $false
    )

    $Deadline =
        [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)

    do {

        $Files = @(
            Get-VcfaContentLibraryItemFiles `
                -Server $Server `
                -Headers $Headers `
                -ItemId $ItemId `
                -IgnoreCertificateErrors $IgnoreCertificateErrors
        )

        $Preferred = $null

        foreach ($FileRecord in $Files) {

            $TransferUrl = [string](
                Get-PropertyValue `
                    -Object $FileRecord `
                    -Names @("transferUrl") `
                    -Default ""
            )

            if ([string]::IsNullOrWhiteSpace($TransferUrl)) {
                continue
            }

            $RemoteName = [string](
                Get-PropertyValue `
                    -Object $FileRecord `
                    -Names @("name") `
                    -Default ""
            )

            if (
                [string]::IsNullOrWhiteSpace($RemoteName) -or
                $RemoteName -eq $LocalFileName
            ) {
                $Preferred = $TransferUrl
                break
            }

            if (-not $Preferred) {
                $Preferred = $TransferUrl
            }
        }

        if ($Preferred) {
            return $Preferred
        }

        Start-Sleep -Seconds $PollIntervalSeconds

    } while (
        [DateTime]::UtcNow -lt $Deadline
    )

    throw (
        "Timed out waiting for a transfer URL for " +
        "content library item '$ItemId'."
    )
}


function Send-VcfaContentLibraryItemFile {

    param(
        [Parameter(Mandatory)]
        [string]$TransferUrl,

        [Parameter(Mandatory)]
        [string]$FilePath,

        [bool]$IgnoreCertificateErrors = $false
    )

    $Params = @{
        Uri         = $TransferUrl
        Method      = "PUT"
        InFile      = $FilePath
        ContentType = "application/octet-stream"
        ErrorAction = "Stop"
    }

    $IWR =
        Get-Command Invoke-WebRequest

    if (
        $IgnoreCertificateErrors -and
        $IWR.Parameters.ContainsKey("SkipCertificateCheck")
    ) {
        $Params.SkipCertificateCheck = $true
    }

    Invoke-WebRequest @Params |
        Out-Null
}


function New-VcfaProviderContentLibraryItemIfMissing {

    param(
        [Parameter(Mandatory)]
        [string]$Server,

        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Headers,

        [Parameter(Mandatory)]
        $Library,

        [Parameter(Mandatory)]
        $Item,

        [Parameter(Mandatory)]
        [string]$ConfigDirectory,

        [bool]$IgnoreCertificateErrors = $false
    )

    $LibraryId =
        Get-VcfaObjectId `
            -Object $Library

    $LibraryName = [string](
        Get-PropertyValue `
            -Object $Library `
            -Names @("name") `
            -Default ""
    )

    if (-not $LibraryId) {
        throw (
            "Unable to determine ID for provider content " +
            "library '$LibraryName'."
        )
    }

    $ItemName = [string](
        Get-PropertyValue `
            -Object $Item `
            -Names @("name") `
            -Default ""
    )

    if ([string]::IsNullOrWhiteSpace($ItemName)) {
        throw (
            "provider_content_libraries[].items[].name is required."
        )
    }

    $Existing =
        Get-VcfaContentLibraryItemByName `
            -Server $Server `
            -Headers $Headers `
            -LibraryId $LibraryId `
            -Name $ItemName `
            -IgnoreCertificateErrors $IgnoreCertificateErrors

    if ($Existing) {

        Write-Skip (
            "Content library item '$ItemName' already exists " +
            "in '$LibraryName'"
        )

        return $Existing
    }

    $ConfiguredPath = [string](
        Get-PropertyValue `
            -Object $Item `
            -Names @("file","file_path") `
            -Default ""
    )

    if ([string]::IsNullOrWhiteSpace($ConfiguredPath)) {
        throw (
            "Content library item '$ItemName' requires file."
        )
    }

    $FilePath =
        Resolve-ConfigFilePath `
            -Path $ConfiguredPath `
            -ConfigDirectory $ConfigDirectory

    if (-not (Test-Path $FilePath -PathType Leaf)) {
        throw (
            "Content library item file not found: $FilePath"
        )
    }

    $ItemType = [string](
        Get-PropertyValue `
            -Object $Item `
            -Names @("item_type","itemType") `
            -Default ""
    )

    if ([string]::IsNullOrWhiteSpace($ItemType)) {
        throw (
            "Content library item '$ItemName' requires item_type."
        )
    }

    $FileInfo =
        Get-Item `
            -LiteralPath $FilePath `
            -ErrorAction Stop

    $Body = [ordered]@{
        name = $ItemName

        contentLibrary = [ordered]@{
            name = $LibraryName
            id   = $LibraryId
        }

        itemType =
            $ItemType

        fileUploadSizeBytes =
            [int64]$FileInfo.Length
    }

    $Description = [string](
        Get-PropertyValue `
            -Object $Item `
            -Names @("description") `
            -Default ""
    )

    if (
        -not [string]::IsNullOrWhiteSpace(
            $Description
        )
    ) {
        $Body.description =
            $Description
    }

    $SystemHeaders =
        New-VcfaSystemHeaders `
            -Headers $Headers

    Write-Create (
        "Content library item '$ItemName' " +
        "in '$LibraryName'"
    )

    $Created =
        Invoke-ApiRest `
            -Uri (
                "https://$Server/cloudapi/v1/" +
                "contentLibraryItems"
            ) `
            -Method POST `
            -Headers $SystemHeaders `
            -Body $Body `
            -IgnoreCertificateErrors $IgnoreCertificateErrors

    if (-not $Created) {
        throw (
            "VCFA did not return the created content library item."
        )
    }

    $ItemId =
        Get-VcfaObjectId `
            -Object $Created

    if (-not $ItemId) {
        throw (
            "Unable to determine ID of newly created " +
            "content library item '$ItemName'."
        )
    }

    $LocalFileName =
        [System.IO.Path]::GetFileName($FilePath)

    Write-Info (
        "Waiting for upload URL for '$LocalFileName'"
    )

    $TransferUrl =
        Wait-VcfaContentLibraryItemTransferUrl `
            -Server $Server `
            -Headers $Headers `
            -ItemId $ItemId `
            -LocalFileName $LocalFileName `
            -IgnoreCertificateErrors $IgnoreCertificateErrors

    Write-Info (
        "Uploading '$LocalFileName' " +
        "($($FileInfo.Length) bytes)"
    )

    Send-VcfaContentLibraryItemFile `
        -TransferUrl $TransferUrl `
        -FilePath $FilePath `
        -IgnoreCertificateErrors $IgnoreCertificateErrors

    Write-Host (
        "[UPLOADED] Content library item '$ItemName'"
    ) -ForegroundColor Green

    return $Created
}


try {
    Write-Info "Connecting to '$($Config.vcenter.server)'"
    $VIServer=Connect-VIServer -Server $Config.vcenter.server -Credential $Credential -ErrorAction Stop
    $RestHeaders=New-VCenterRestSession -Server $Config.vcenter.server -Username $Config.vcenter.username -Password $Password -IgnoreCertificateErrors $IgnoreCertErrors

    # ========================================================
    # vCenter sections are nested under Config.vcenter
    # ========================================================

    $VCenterCategories = @()
    $VCenterAssignments = @()
    $VCenterPolicies = @()

    if (Test-Property $Config.vcenter "categories") {
        $VCenterCategories = @($Config.vcenter.categories)
    }

    if (Test-Property $Config.vcenter "assignments") {
        $VCenterAssignments = @($Config.vcenter.assignments)
    }

    if (Test-Property $Config.vcenter "policies") {
        $VCenterPolicies = @($Config.vcenter.policies)
    }

    if ($VCenterCategories.Count -gt 0) {
        Write-Host ""
        Write-Host "========================================"
        Write-Host " vCenter Categories / Tags"
        Write-Host "========================================"

        foreach ($CategoryConfig in $VCenterCategories) {
            $Description = if (Test-Property $CategoryConfig "description") {
                [string]$CategoryConfig.description
            }
            else {
                ""
            }

            $Cardinality = if (Test-Property $CategoryConfig "cardinality") {
                [string]$CategoryConfig.cardinality
            }
            else {
                "Single"
            }

            $EntityTypes = if (Test-Property $CategoryConfig "entity_types") {
                @($CategoryConfig.entity_types)
            }
            else {
                @()
            }

            $Category = Get-OrCreateTagCategory `
                -Name $CategoryConfig.name `
                -Description $Description `
                -Cardinality $Cardinality `
                -EntityTypes $EntityTypes

            if (Test-Property $CategoryConfig "tags") {
                foreach ($TagConfig in @($CategoryConfig.tags)) {
                    $TagDescription = if (Test-Property $TagConfig "description") {
                        [string]$TagConfig.description
                    }
                    else {
                        ""
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
        Write-Skip "No vCenter categories configured"
    }

    if ($VCenterAssignments.Count -gt 0) {
        Write-Host ""
        Write-Host "========================================"
        Write-Host " vCenter Tag Assignments"
        Write-Host "========================================"

        foreach ($Assignment in $VCenterAssignments) {
            $Entity = Get-vSphereObject `
                -Type $Assignment.type `
                -Name $Assignment.name

            foreach ($TagConfig in @($Assignment.tags)) {
                $Tag = Get-ExactTag `
                    -CategoryName $TagConfig.category `
                    -TagName $TagConfig.tag

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
        Write-Skip "No vCenter tag assignments configured"
    }

    if ($VCenterPolicies.Count -gt 0) {
        Write-Host ""
        Write-Host "========================================"
        Write-Host " vCenter Compute Policies"
        Write-Host "========================================"

        foreach ($Policy in $VCenterPolicies) {
            New-RestComputePolicyIfMissing `
                -Server $Config.vcenter.server `
                -Headers $RestHeaders `
                -Policy $Policy `
                -IgnoreCertificateErrors $IgnoreCertErrors
        }
    }
    else {
        Write-Skip "No vCenter compute policies configured"
    }


    # ========================================================
    # VCFA sections are nested under Config.vcfa
    # ========================================================

    $VcfaInfrastructurePolicies = @()

    if (
        $VcfaConfigured -and
        (Test-Property $Config.vcfa "infrastructure_policies")
    ) {
        $VcfaInfrastructurePolicies =
            @($Config.vcfa.infrastructure_policies)
    }

    if (
        $VcfaConfigured -and
        $VcfaInfrastructurePolicies.Count -gt 0
    ) {
        Write-Host ""
        Write-Host "========================================"
        Write-Host " VCFA Infrastructure Policies"
        Write-Host "========================================"

        $ComputePolicySyncTimeout = 300
        $ComputePolicySyncInterval = 10

        if (Test-Property $Config.vcfa "compute_policy_sync_timeout_seconds") {
            $ComputePolicySyncTimeout =
                [int]$Config.vcfa.compute_policy_sync_timeout_seconds
        }

        if (Test-Property $Config.vcfa "compute_policy_sync_poll_seconds") {
            $ComputePolicySyncInterval =
                [int]$Config.vcfa.compute_policy_sync_poll_seconds
        }

        foreach ($Policy in $VcfaInfrastructurePolicies) {

            if (Test-Property $Policy "vc_compute_policy_name") {

                $VCPolicy = Get-RestComputePolicy `
                    -Server $Config.vcenter.server `
                    -Headers $RestHeaders `
                    -Name $Policy.vc_compute_policy_name `
                    -IgnoreCertificateErrors $IgnoreCertErrors

                if (-not $VCPolicy) {
                    throw (
                        "Referenced vCenter compute policy " +
                        "'$($Policy.vc_compute_policy_name)' does not exist."
                    )
                }

                # VCFA discovers vCenter compute policies asynchronously.
                # Wait until the policy is visible to VCFA before creating
                # the VCFA infrastructure policy.
                Wait-VcfaVCenterComputePolicy `
                    -Server $Config.vcfa.server `
                    -Headers $VcfaHeaders `
                    -PolicyName $Policy.vc_compute_policy_name `
                    -VCenterName $Config.vcenter.server `
                    -TimeoutSeconds $ComputePolicySyncTimeout `
                    -PollIntervalSeconds $ComputePolicySyncInterval `
                    -IgnoreCertificateErrors $VcfaIgnoreCertErrors |
                Out-Null
            }

            New-VcfaInfrastructurePolicyIfMissing `
                -Server $Config.vcfa.server `
                -Headers $VcfaHeaders `
                -Policy $Policy `
                -IgnoreCertificateErrors $VcfaIgnoreCertErrors
        }
    }
    elseif ($VcfaConfigured) {
        Write-Skip "No VCFA infrastructure policies configured"
    }


    # ========================================================
    # VCFA Provider Content Libraries - nested under vcfa
    # ========================================================

    $ConfiguredContentLibraries = @()

    if (
        $VcfaConfigured -and
        (Test-Property $Config.vcfa "provider_content_libraries")
    ) {
        $ConfiguredContentLibraries =
            @($Config.vcfa.provider_content_libraries)
    }

    if (
        $VcfaConfigured -and
        $ConfiguredContentLibraries.Count -gt 0
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " VCFA Provider Content Libraries"
        Write-Host "========================================"

        foreach ($LibraryConfig in $ConfiguredContentLibraries) {

            $ConfiguredLibraryName = [string](
                Get-PropertyValue `
                    -Object $LibraryConfig `
                    -Names @("name") `
                    -Default "<unnamed>"
            )

            $LibraryIsSubscribed = $false

            if (Test-Property $LibraryConfig "is_subscribed") {
                $LibraryIsSubscribed =
                    [bool]$LibraryConfig.is_subscribed
            }

            $SubscriptionUrl = ""

            if (
                $LibraryIsSubscribed -and
                (Test-Property $LibraryConfig "subscription") -and
                $null -ne $LibraryConfig.subscription -and
                (Test-Property $LibraryConfig.subscription "url")
            ) {
                $SubscriptionUrl =
                    [string]$LibraryConfig.subscription.url
            }

            $HasUsableSubscription = (
                $LibraryIsSubscribed -and
                -not [string]::IsNullOrWhiteSpace(
                    $SubscriptionUrl
                )
            )

            $ConfiguredItems = @()

            if (
                (Test-Property $LibraryConfig "items") -and
                $null -ne $LibraryConfig.items
            ) {
                $ConfiguredItems =
                    @($LibraryConfig.items)
            }

            # ------------------------------------------------
            # Rule 1
            # is_subscribed=true and no usable subscription:
            # skip the content library completely.
            # ------------------------------------------------

            if (
                $LibraryIsSubscribed -and
                -not $HasUsableSubscription
            ) {

                Write-Skip (
                    "Provider content library '$ConfiguredLibraryName' " +
                    "has is_subscribed=true but no usable subscription; " +
                    "skipping"
                )

                continue
            }

            # ------------------------------------------------
            # Create the library.
            #
            # For is_subscribed=false the subscription object is
            # deliberately ignored by the create function.
            # ------------------------------------------------

            $Library =
                New-VcfaProviderContentLibraryIfMissing `
                    -Server $Config.vcfa.server `
                    -Headers $VcfaHeaders `
                    -Library $LibraryConfig `
                    -IgnoreCertificateErrors $VcfaIgnoreCertErrors

            if (-not $Library) {
                continue
            }

            # ------------------------------------------------
            # Rule 2
            # is_subscribed=true + subscription:
            # library is subscription-driven; ignore local items.
            # ------------------------------------------------

            if ($HasUsableSubscription) {

                if ($ConfiguredItems.Count -gt 0) {
                    Write-Skip (
                        "Ignoring $($ConfiguredItems.Count) local item(s) " +
                        "for subscribed content library " +
                        "'$ConfiguredLibraryName'"
                    )
                }

                continue
            }

            # ------------------------------------------------
            # Rule 3
            # is_subscribed=false + items:
            # ignore subscription and create/upload items.
            # ------------------------------------------------

            if ($ConfiguredItems.Count -gt 0) {

                foreach ($ItemConfig in $ConfiguredItems) {

                    New-VcfaProviderContentLibraryItemIfMissing `
                        -Server $Config.vcfa.server `
                        -Headers $VcfaHeaders `
                        -Library $Library `
                        -Item $ItemConfig `
                        -ConfigDirectory $ConfigDirectory `
                        -IgnoreCertificateErrors $VcfaIgnoreCertErrors |
                    Out-Null
                }

                continue
            }

            # ------------------------------------------------
            # Rules 4 & 5
            # is_subscribed=false + no items:
            # create library only, irrespective of whether a
            # subscription object is present or absent.
            # ------------------------------------------------

            Write-Skip (
                "No local items configured for content library " +
                "'$ConfiguredLibraryName'; library created without items"
            )
        }
    }
    elseif ($VcfaConfigured) {
        Write-Skip "No VCFA provider content libraries configured"
    }



# ============================================================
# VCFA Provider Content Library Items
#
# Supports idempotent upload of a single local file per item.
# The Content Library Item is created first; VCFA then exposes
# a transferUrl via the item-files endpoint. The local file is
# uploaded directly to that transfer URL.
# ============================================================


    # ========================================================
    # VCFA Tenants - nested under vcfa
    # ========================================================

    $VcfaTenants = @()

    if (
        $VcfaConfigured -and
        (Test-Property $Config.vcfa "tenants")
    ) {
        $VcfaTenants = @($Config.vcfa.tenants)
    }

    if (
        $VcfaConfigured -and
        $VcfaTenants.Count -gt 0
    ) {

        Write-Host ""
        Write-Host "========================================"
        Write-Host " VCFA Tenants"
        Write-Host "========================================"

        foreach ($TenantConfig in $VcfaTenants) {

            $Tenant = Get-OrCreateVcfaTenant `
                -Server $Config.vcfa.server `
                -Headers $VcfaHeaders `
                -Tenant $TenantConfig `
                -IgnoreCertificateErrors $VcfaIgnoreCertErrors

            $TenantId = Get-VcfaObjectId -Object $Tenant

            Write-Info (
                "Tenant '$($Tenant.name)' [$TenantId]"
            )

            $RegionQuota = $null
            $Region = $null

            if (Test-Property $TenantConfig "regional_quota") {

                $RegionQuota = New-VcfaRegionQuotaIfMissing `
                    -Server $Config.vcfa.server `
                    -Headers $VcfaHeaders `
                    -Tenant $Tenant `
                    -QuotaConfig $TenantConfig.regional_quota `
                    -IgnoreCertificateErrors $VcfaIgnoreCertErrors

                $Region = Get-VcfaRegion `
                    -Server $Config.vcfa.server `
                    -Headers $VcfaHeaders `
                    -Name $TenantConfig.regional_quota.region `
                    -IgnoreCertificateErrors $VcfaIgnoreCertErrors
            }
            else {
                Write-Skip (
                    "No regional quota configured for '$($Tenant.name)'"
                )
            }

            if (
                $RegionQuota -and
                (Test-Property $TenantConfig "resources")
            ) {
                $Resources = $TenantConfig.resources

                if (Test-Property $Resources "vm_classes") {
                    Set-VcfaVdcVmClasses `
                        -Server $Config.vcfa.server `
                        -Headers $VcfaHeaders `
                        -Vdc $RegionQuota `
                        -Config $Resources.vm_classes `
                        -IgnoreCertificateErrors $VcfaIgnoreCertErrors
                }

                if (Test-Property $Resources "storage_classes") {
                    Set-VcfaVdcStorageClasses `
                        -Server $Config.vcfa.server `
                        -Headers $VcfaHeaders `
                        -Vdc $RegionQuota `
                        -Config $Resources.storage_classes `
                        -IgnoreCertificateErrors $VcfaIgnoreCertErrors
                }

                if (Test-Property $Resources "infra_policies") {
                    Set-VcfaVdcInfraPolicies `
                        -Server $Config.vcfa.server `
                        -Headers $VcfaHeaders `
                        -Vdc $RegionQuota `
                        -Config $Resources.infra_policies `
                        -IgnoreCertificateErrors $VcfaIgnoreCertErrors
                }
            }

            if (
                $Region -and
                (Test-Property $TenantConfig "external_connection") -and
                $null -ne $TenantConfig.external_connection
            ) {
                Set-VcfaExternalConnection `
                    -Server $Config.vcfa.server `
                    -Headers $VcfaHeaders `
                    -Tenant $Tenant `
                    -Region $Region `
                    -ExternalConnection $TenantConfig.external_connection `
                    -IgnoreCertificateErrors $VcfaIgnoreCertErrors |
                Out-Null
            }

            # first_user is intentionally ignored.
        }
    }
    elseif ($VcfaConfigured) {
        Write-Skip "No VCFA tenants configured"
    }

    Write-Host ""; Write-Host "Configuration completed successfully." -ForegroundColor Green
}
finally {
    if ($RestHeaders) { try { Invoke-ApiRest -Uri "https://$($Config.vcenter.server)/api/session" -Method DELETE -Headers $RestHeaders -IgnoreCertificateErrors $IgnoreCertErrors | Out-Null } catch { Write-Warn "Unable to close vCenter REST session" } }
    if ($VIServer) { Disconnect-VIServer -Server $VIServer -Confirm:$false }
}
