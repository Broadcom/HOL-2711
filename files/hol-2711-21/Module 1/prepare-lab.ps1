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
    param([object]$Object,[string]$Name)
    return ($null -ne $Object -and $Object.PSObject.Properties.Name -contains $Name)
}

function Get-VcfaObjectReference {
    param([Parameter(Mandatory)][object]$Object,[Parameter(Mandatory)][string[]]$PropertyNames)
    foreach ($PropertyName in $PropertyNames) {
        if (-not (Test-Property $Object $PropertyName)) { continue }
        $Reference = $Object.$PropertyName
        if ($null -eq $Reference) { continue }
        $Id = $null; $Name = $null
        if (Test-Property $Reference "id") { $Id = [string]$Reference.id }
        elseif (Test-Property $Reference "urn") { $Id = [string]$Reference.urn }
        if (Test-Property $Reference "name") { $Name = [string]$Reference.name }
        elseif (Test-Property $Reference "displayName") { $Name = [string]$Reference.displayName }
        if ($Id -or $Name) { return [PSCustomObject]@{ Id = $Id; Name = $Name } }
    }
    return $null
}

function Get-VcfaObjectId {
    param([Parameter(Mandatory)][object]$Object)
    foreach ($Name in @("id","urn","supervisorId","zoneId","regionId")) {
        if (Test-Property $Object $Name) {
            $Value = $Object.$Name
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
function New-VcfaTenantHeaders {
    param([System.Collections.IDictionary]$Headers,[string]$OrgId)
    $Result=[System.Collections.Generic.Dictionary[string,string]]::new()
    foreach ($Key in $Headers.Keys) { $Result.Add([string]$Key,[string]$Headers[$Key]) }
    $Result["X-VMWARE-VCLOUD-TENANT-CONTEXT"]=$OrgId
    return $Result
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
                $AvailableZoneName=$null
                if (Test-Property $AvailableZone "name") { $AvailableZoneName=[string]$AvailableZone.name }
                if (-not $AvailableZoneName -and (Test-Property $AvailableZone "zone") -and $null -ne $AvailableZone.zone -and (Test-Property $AvailableZone.zone "name")) { $AvailableZoneName=[string]$AvailableZone.zone.name }
                if ($AvailableZoneName -eq $ZoneConfig.zone) { $Zone=$AvailableZone; break }
            }
            if (-not $Zone) { throw "Zone '$($ZoneConfig.zone)' not found in region '$($Region.name)'." }
            $ZoneName=$null; if (Test-Property $Zone "name") { $ZoneName=[string]$Zone.name }
            $ZoneId=Get-VcfaObjectId -Object $Zone
            if (Test-Property $Zone "zone" -and $null -ne $Zone.zone) {
                if (Test-Property $Zone.zone "name") { $ZoneName=[string]$Zone.zone.name }
                $NestedZoneId=Get-VcfaObjectId -Object $Zone.zone; if ($NestedZoneId) { $ZoneId=$NestedZoneId }
            }
            if (-not $ZoneId) { throw "Unable to determine ID for zone '$($ZoneConfig.zone)'." }
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

    if (-not (Test-Property $ExternalConnection "name")) {
        throw "external_connection.name is required."
    }

    if (-not (Test-Property $ExternalConnection "cluster")) {
        throw "external_connection.cluster is required."
    }

    $ConnectionName = [string]$ExternalConnection.name
    $ClusterName = [string]$ExternalConnection.cluster
    $IsDistributed = $false

    if (Test-Property $ExternalConnection "distributed") {
        $IsDistributed = [bool]$ExternalConnection.distributed
    }

    if (-not $IsDistributed) {
        throw "This script currently supports external_connection.distributed=true only."
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
    Write-Info "Resolving VNA Cluster '$ClusterName'"

    $VnaCluster = $null
    foreach ($Candidate in @(Get-VcfaVnaClusters -Server $Server -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors)) {
        if (-not (Test-Property $Candidate "name")) { continue }
        if ([string]$Candidate.name -ne $ClusterName) { continue }

        $CandidateRegionRef = Get-VcfaObjectReference -Object $Candidate -PropertyNames @("regionRef","region")
        if ($CandidateRegionRef -and $CandidateRegionRef.Id -ne $RegionId) { continue }

        $VnaCluster = $Candidate
        break
    }

    if (-not $VnaCluster) {
        throw "VNA Cluster '$ClusterName' not found in region '$($Region.name)'."
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

    Write-Skip "External connection '$($ExternalConnection.name)' configured through Regional Networking Setting"
    return $Setting
}

function Get-VcfaRoles { param([string]$Server,[System.Collections.IDictionary]$Headers,[bool]$IgnoreCertificateErrors=$false) return Get-VcfaPagedValues -Server $Server -Path "/cloudapi/1.0.0/roles" -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors }
function Get-VcfaUsers { param([string]$Server,[System.Collections.IDictionary]$Headers,[bool]$IgnoreCertificateErrors=$false) return Get-VcfaPagedValues -Server $Server -Path "/cloudapi/1.0.0/users" -Headers $Headers -IgnoreCertificateErrors $IgnoreCertificateErrors }
function New-VcfaFirstUserIfMissing {
    param(
        [string]$Server,
        [System.Collections.IDictionary]$Headers,
        $Tenant,
        $UserConfig,
        [string]$ConfigDirectory,
        [bool]$IgnoreCertificateErrors=$false
    )

    $TenantId = Get-VcfaObjectId -Object $Tenant

    if (-not $TenantId) {
        throw "Unable to determine tenant ID for '$($Tenant.name)'."
    }

    # Tenant-context headers are used for tenant-local discovery only.
    $TenantHeaders = New-VcfaTenantHeaders `
        -Headers $Headers `
        -OrgId $TenantId

    # Check whether the user already exists in the target tenant.
    $Existing = Get-VcfaUsers `
        -Server $Server `
        -Headers $TenantHeaders `
        -IgnoreCertificateErrors $IgnoreCertificateErrors |
        Where-Object { $_.username -eq $UserConfig.username } |
        Select-Object -First 1

    if ($Existing) {
        Write-Skip "VCFA user '$($UserConfig.username)' already exists in tenant '$($Tenant.name)'"
        return $Existing
    }

    $ProviderType = if (Test-Property $UserConfig "provider_type") {
        [string]$UserConfig.provider_type
    }
    else {
        "LOCAL"
    }

    $Body = [ordered]@{
        username     = $UserConfig.username
        providerType = $ProviderType
        enabled      = $true
        orgEntityRef = [ordered]@{
            name = $Tenant.name
            id   = $TenantId
        }
    }

    if (Test-Property $UserConfig "enabled") {
        $Body.enabled = [bool]$UserConfig.enabled
    }

    foreach ($Mapping in @(
        @{Source="given_name";  Target="givenName"},
        @{Source="family_name"; Target="familyName"},
        @{Source="full_name";   Target="fullName"},
        @{Source="email";       Target="email"},
        @{Source="description"; Target="description"}
    )) {
        if (Test-Property $UserConfig $Mapping.Source) {
            $Value = $UserConfig.($Mapping.Source)
            if (-not [string]::IsNullOrWhiteSpace([string]$Value)) {
                $Body[$Mapping.Target] = $Value
            }
        }
    }

    if ($ProviderType.ToUpper() -eq "LOCAL") {
        if (-not (Test-Property $UserConfig "password_file")) {
            throw "LOCAL user '$($UserConfig.username)' requires password_file."
        }

        $UserPasswordFile = Resolve-ConfigFilePath `
            -Path $UserConfig.password_file `
            -ConfigDirectory $ConfigDirectory

        if (-not (Test-Path $UserPasswordFile)) {
            throw "User password file not found: $UserPasswordFile"
        }

        $UserPassword = (Get-Content -Path $UserPasswordFile -Raw).Trim()

        if ([string]::IsNullOrWhiteSpace($UserPassword)) {
            throw "Password file for user '$($UserConfig.username)' is empty."
        }

        $Body.password = $UserPassword
    }

    # Resolve tenant-local role using tenant context.
    if (Test-Property $UserConfig "role") {
        Write-Info "Resolving role '$($UserConfig.role)' in tenant '$($Tenant.name)'"

        $Role = Get-VcfaRoles `
            -Server $Server `
            -Headers $TenantHeaders `
            -IgnoreCertificateErrors $IgnoreCertificateErrors |
            Where-Object { $_.name -eq $UserConfig.role } |
            Select-Object -First 1

        if (-not $Role) {
            throw "VCFA role '$($UserConfig.role)' was not found in tenant '$($Tenant.name)'."
        }

        $RoleId = Get-VcfaObjectId -Object $Role

        if (-not $RoleId) {
            throw "Unable to determine ID for role '$($Role.name)'."
        }

        Write-Info "Using role '$($Role.name)' [$RoleId]"

        $Body.roleEntityRefs = @(
            [ordered]@{
                name = $Role.name
                id   = $RoleId
            }
        )
    }

    Write-Create "VCFA user '$($UserConfig.username)' in tenant '$($Tenant.name)'"
    Write-Info "Creating user using provider context with explicit orgEntityRef"

    $Uri = "https://$Server/cloudapi/1.0.0/users"

    try {
        # IMPORTANT: create with PROVIDER headers, not tenant-context headers.
        Invoke-ApiRest `
            -Uri $Uri `
            -Method POST `
            -Headers $Headers `
            -Body $Body `
            -IgnoreCertificateErrors $IgnoreCertificateErrors |
            Out-Null

        Write-Host "[CREATED] User '$($UserConfig.username)' in tenant '$($Tenant.name)'" -ForegroundColor Green
    }
    catch {
        $Status = Get-HttpStatusCode $_

        Write-Host ""
        Write-Host "VCFA user creation failed" -ForegroundColor Red
        Write-Host "User:     $($UserConfig.username)"
        Write-Host "Tenant:   $($Tenant.name)"
        Write-Host "TenantID: $TenantId"
        Write-Host "HTTP:     $Status"
        Write-Host "Endpoint: $Uri"
        Write-Host "Context:  Provider (orgEntityRef supplied in body)"

        Write-Host ""
        Write-Host "Payload:"

        # Never expose the password in diagnostics.
        $DebugBody = $Body | ConvertTo-Json -Depth 20 | ConvertFrom-Json
        if (Test-Property $DebugBody "password") {
            $DebugBody.password = "********"
        }
        Write-Host ($DebugBody | ConvertTo-Json -Depth 20)

        Write-Host ""
        Write-Host "Response:"
        Write-Host (Get-RestErrorDetail $_)

        throw
    }
}


$ResolvedConfig=(Resolve-Path $ConfigFile).Path
$ConfigDirectory=Split-Path -Parent $ResolvedConfig
Write-Info "Loading '$ResolvedConfig'"
$Config=Get-Content -Path $ResolvedConfig -Raw | ConvertFrom-Json

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
try {
    Write-Info "Connecting to '$($Config.vcenter.server)'"
    $VIServer=Connect-VIServer -Server $Config.vcenter.server -Credential $Credential -ErrorAction Stop
    $RestHeaders=New-VCenterRestSession -Server $Config.vcenter.server -Username $Config.vcenter.username -Password $Password -IgnoreCertificateErrors $IgnoreCertErrors

    if ((Test-Property $Config "categories") -and $Config.categories -and (@($Config.categories).Count -gt 0)) {
        Write-Host ""; Write-Host "========================================"; Write-Host " vCenter Categories / Tags"; Write-Host "========================================"
        foreach ($CategoryConfig in @($Config.categories)) {
            $Description=if (Test-Property $CategoryConfig "description") {[string]$CategoryConfig.description} else {""}
            $Cardinality=if (Test-Property $CategoryConfig "cardinality") {[string]$CategoryConfig.cardinality} else {"Single"}
            $EntityTypes=if (Test-Property $CategoryConfig "entity_types") {@($CategoryConfig.entity_types)} else {@()}
            $Category=Get-OrCreateTagCategory -Name $CategoryConfig.name -Description $Description -Cardinality $Cardinality -EntityTypes $EntityTypes
            if (Test-Property $CategoryConfig "tags") { foreach ($TagConfig in @($CategoryConfig.tags)) { $TagDescription=if (Test-Property $TagConfig "description") {[string]$TagConfig.description} else {""}; Get-OrCreateTag -Name $TagConfig.name -Category $Category -Description $TagDescription | Out-Null } }
        }
    } else { Write-Skip "No vCenter categories configured" }

    if ((Test-Property $Config "assignments") -and $Config.assignments -and (@($Config.assignments).Count -gt 0)) {
        Write-Host ""; Write-Host "========================================"; Write-Host " vCenter Tag Assignments"; Write-Host "========================================"
        foreach ($Assignment in @($Config.assignments)) { $Entity=Get-vSphereObject -Type $Assignment.type -Name $Assignment.name; foreach ($TagConfig in @($Assignment.tags)) { $Tag=Get-ExactTag -CategoryName $TagConfig.category -TagName $TagConfig.tag; if (-not $Tag) { throw "Tag '$($TagConfig.category)/$($TagConfig.tag)' does not exist." }; Set-TagIfMissing -Entity $Entity -Tag $Tag } }
    } else { Write-Skip "No vCenter tag assignments configured" }

    if ((Test-Property $Config "policies") -and $Config.policies -and (@($Config.policies).Count -gt 0)) {
        Write-Host ""; Write-Host "========================================"; Write-Host " vCenter Compute Policies"; Write-Host "========================================"
        foreach ($Policy in @($Config.policies)) { New-RestComputePolicyIfMissing -Server $Config.vcenter.server -Headers $RestHeaders -Policy $Policy -IgnoreCertificateErrors $IgnoreCertErrors }
    } else { Write-Skip "No vCenter compute policies configured" }

    if ($VcfaConfigured -and (Test-Property $Config "infrastructure_policies") -and $Config.infrastructure_policies -and (@($Config.infrastructure_policies).Count -gt 0)) {
        Write-Host ""; Write-Host "========================================"; Write-Host " VCFA Infrastructure Policies"; Write-Host "========================================"
        foreach ($Policy in @($Config.infrastructure_policies)) { if (Test-Property $Policy "vc_compute_policy_name") { $VCPolicy=Get-RestComputePolicy -Server $Config.vcenter.server -Headers $RestHeaders -Name $Policy.vc_compute_policy_name -IgnoreCertificateErrors $IgnoreCertErrors; if (-not $VCPolicy) { throw "Referenced vCenter compute policy '$($Policy.vc_compute_policy_name)' does not exist." } }; New-VcfaInfrastructurePolicyIfMissing -Server $Config.vcfa.server -Headers $VcfaHeaders -Policy $Policy -IgnoreCertificateErrors $VcfaIgnoreCertErrors }
    } elseif ($VcfaConfigured) { Write-Skip "No VCFA infrastructure policies configured" }

    if ($VcfaConfigured -and (Test-Property $Config "tenants") -and $Config.tenants -and (@($Config.tenants).Count -gt 0)) {
        Write-Host ""; Write-Host "========================================"; Write-Host " VCFA Tenants"; Write-Host "========================================"
        foreach ($TenantConfig in @($Config.tenants)) {
            $Tenant=Get-OrCreateVcfaTenant -Server $Config.vcfa.server -Headers $VcfaHeaders -Tenant $TenantConfig -IgnoreCertificateErrors $VcfaIgnoreCertErrors
            $TenantId=Get-VcfaObjectId -Object $Tenant; Write-Info "Tenant '$($Tenant.name)' [$TenantId]"
            $RegionQuota=$null; $Region=$null
            if (Test-Property $TenantConfig "regional_quota") { $RegionQuota=New-VcfaRegionQuotaIfMissing -Server $Config.vcfa.server -Headers $VcfaHeaders -Tenant $Tenant -QuotaConfig $TenantConfig.regional_quota -IgnoreCertificateErrors $VcfaIgnoreCertErrors; $Region=Get-VcfaRegion -Server $Config.vcfa.server -Headers $VcfaHeaders -Name $TenantConfig.regional_quota.region -IgnoreCertificateErrors $VcfaIgnoreCertErrors } else { Write-Skip "No regional quota configured for '$($Tenant.name)'" }
            if ($RegionQuota -and (Test-Property $TenantConfig "resources")) { $Resources=$TenantConfig.resources; if (Test-Property $Resources "vm_classes") { Set-VcfaVdcVmClasses -Server $Config.vcfa.server -Headers $VcfaHeaders -Vdc $RegionQuota -Config $Resources.vm_classes -IgnoreCertificateErrors $VcfaIgnoreCertErrors }; if (Test-Property $Resources "storage_classes") { Set-VcfaVdcStorageClasses -Server $Config.vcfa.server -Headers $VcfaHeaders -Vdc $RegionQuota -Config $Resources.storage_classes -IgnoreCertificateErrors $VcfaIgnoreCertErrors }; if (Test-Property $Resources "infra_policies") { Set-VcfaVdcInfraPolicies -Server $Config.vcfa.server -Headers $VcfaHeaders -Vdc $RegionQuota -Config $Resources.infra_policies -IgnoreCertificateErrors $VcfaIgnoreCertErrors } }
            if ($Region -and (Test-Property $TenantConfig "external_connection") -and $null -ne $TenantConfig.external_connection) { Set-VcfaExternalConnection -Server $Config.vcfa.server -Headers $VcfaHeaders -Tenant $Tenant -Region $Region -ExternalConnection $TenantConfig.external_connection -IgnoreCertificateErrors $VcfaIgnoreCertErrors | Out-Null }
            if (Test-Property $TenantConfig "first_user") { New-VcfaFirstUserIfMissing -Server $Config.vcfa.server -Headers $VcfaHeaders -Tenant $Tenant -UserConfig $TenantConfig.first_user -ConfigDirectory $ConfigDirectory -IgnoreCertificateErrors $VcfaIgnoreCertErrors } else { Write-Skip "No first user configured for '$($Tenant.name)'" }
        }
    } elseif ($VcfaConfigured) { Write-Skip "No VCFA tenants configured" }

    Write-Host ""; Write-Host "Configuration completed successfully." -ForegroundColor Green
}
finally {
    if ($RestHeaders) { try { Invoke-ApiRest -Uri "https://$($Config.vcenter.server)/api/session" -Method DELETE -Headers $RestHeaders -IgnoreCertificateErrors $IgnoreCertErrors | Out-Null } catch { Write-Warn "Unable to close vCenter REST session" } }
    if ($VIServer) { Disconnect-VIServer -Server $VIServer -Confirm:$false }
}
