# simple-pushgateway

## Installing the Chart

The chart can be installed as follows:

```console
$ helm repo add simple-pushgateway https://pixelfederation.github.io/simple-pushgateway
$ helm --namespace=simple-pushgateway install simple-pushgateway pixelfederation/simple-pushgateway
```

To uninstall/delete the `simple-pushgateway` deployment:

```console
$ helm uninstall simple-pushgateway
```
The command removes all the Kubernetes components associated with the chart and deletes the release.
