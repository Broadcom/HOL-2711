# console
ssh-copy-id root@o11n-01a

# manager
scp /vpodrepo/2027-labs/2711/certs/o11n-01a/o11n-01a.site-a.vcf.lab.pem root@o11n-01a.site-a.vcf.lab:/root/cert.pem

# o11n-01a
vracli certificate ingress --set /root/cert.pem --sha256 26c0a5a01d06caec635dcf17a180407c409de07eba95bc4c4d65413ee0db1eec
/opt/scripts/deploy.sh

vracli vro authentication set --force --ignore-certificate --provider=tm --username="admin" --hostname="https://auto-a.site-a.vcf.lab" --tenant="scitech"
/opt/scripts/deploy.sh