# Deploy Runbook

The payment service ships to the Frankfurt region every weekday at 04:30 UTC
through the meridian pipeline. Rollback is one command away via the same
pipeline manifest. On-call rotation owns the pager for failed releases.
