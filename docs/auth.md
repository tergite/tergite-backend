# Authentication and Authorization

Most of the heavy lifting for authentication is carried out by [MSS](https://bitbucket.org/qtlteam/tergite-mss)
Have a look at the [MSS auth documentation](https://bitbucket.org/qtlteam/tergite-mss/src/main/docs/auth.md)
to understand the authentication flow.

## Authentication between backend and frontend

### How MSS authenticates BCC's

BCC should be able to send events like 'initialized', 'recalibrated', 'job_updated', along with fresh data like
device parameters, calibration data and job results.

- In order for only authentic backends to send data to MSS, we use BCC's `PRIVATE_KEY_FILE` (RSA) PEM file to sign websocket connections to 
MSS.
- MSS on the other end has all BCC's `PUBLIC_KEY_FILE` PEM files and a list of all allowed BCC's in its config file.
- Each websocket connection to the `/devices/ws/{device-name}` must have a number of custom headers and a signature
    - `x-id` (contains name of device)
    - `x-request-id` (contains random secret that is always different for each request)
    - `x-signature` (contains the `PRIVATE_KEY_FILE`-signed secret)
    - `x-timestamp` (contains the timestamp when the signature was made. the signature is valid for only a given time-to-live)
- MSS checks this signature using that BCC's `PUBLIC_KEY_FILE` PEM file.  
  It must have been generated with the private key of the device of the given `{device-name}` and it must not be expired.  
  Otherwise, the connection is rejected.

### How BCC authenticates MSS

MSS should be able to send data and request for other data from BCC. 

- In order for only authentic MSS to talk to BCC, MSS uses `MSS_PRIVATE_KEY_PATH` (RSA) PEM file to sign a secret
- The secret is attached to the headers of the request. The request must have at least the following headers
    - `x-mss-user-id` (contains id of the user who from whom the original request to MSS was made, or none if no user was involved)
    - `x-mss-request-id` (contains random secret that is always different for each request)
    - `x-mss-signature` (contains the `MSS_PRIVATE_KEY_PATH`-signed secret)
    - `x-mss-timestamp` (contains the timestamp when the signature was made. the signature is valid for only a given time-to-live)
    - `x-mss-is-admin` (contains the 'True' if the user, from whom the original request in MSS came)
- BCC validates that this signature using the MSS's `MSS_PRIVATE_KEY_PATH` (RSA) PEM file. It rejects requests with invalid or expired signatures.
