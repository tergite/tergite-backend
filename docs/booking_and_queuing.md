# Booking and queueing

By default, jobs run on a first-come-first-serve basis, but it is also possible to 
book a time slot to accept only one user's jobs and waitlist other users' jobs.

## Possible operations

- [x] User creation via MSS
- [x] Booking creation
- [x] User login via MSS to get JWT token encrypted with MSS public key
- [x] Submit jobs with active booking; waitlisting all jobs of non-bookers, and queueing those of booker
- [x] Run waitlisted jobs when booking becomes idle
- [x] Creation of bookings overlapping with other bookings is not permitted
- [x] Creation of bookings beyond the maximum per day is not permitted.
- [x] User can delete their own booking as long as it has not yet expired.
- [x] User can view all other user's bookings (paginated)
- [x] Admin user can cancel any user's booking
- [x] User can cancel a job that is yet to complete as long as it belongs to them.
- [x] Can create root user (is admin) only once for a given app instance
- [x] Admin user can delete any user (automatically cancelling all their jobs and bookings)
- [x] User can delete their account, (automatically cancelling all their jobs and bookings)
- [x] User can view their own user profiles
- [x] Admin can view all users (paginated)
- [x] User can view their own jobs, both many and single

## Dependencies

- [Redis](https://redis.io/)
- [SQLite](https://www.sqlite.org/)
- [rq](https://python-rq.org/)


## Operations (REST API)

- Ensure you have [Anaconda/miniconda](https://www.anaconda.com/docs/getting-started/miniconda/install) 
 and [redis server/stack](https://redis.io/docs/latest/operate/oss_and_stack/install/archive/install-redis/) installed.

_Note: the [RQ dashboard](https://github.com/nvie/rq-dashboard) will not work if `dev` optional dependencies are not included_  

- Open another shell start the [RQ dashboard](https://github.com/nvie/rq-dashboard)

```shell
conda activate bookenv
rq-dashboard
```

- Through, MSS, create users e.g.

```shell
curl -L 'http://127.0.0.1:5000/users' \
-H 'Content-Type: application/json' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420' \
-H 'x-mss-is-admin: True' \
--data-raw '{
    "id": "0f29a380-3a02-4c6f-86f9-340574d747aa",
    "name": "John Doe",
    "email": "johndoe@example.com",
    "password": "some-password-for-john"
}'
```

Responding with: 

```json
{
    "id": "0f29a380-3a02-4c6f-86f9-340574d747aa",
    "name": "John Doe",
    "email": "johndoe@example.com"
}
```

- Through MSS, create a booking for one user e.g..

```shell
curl -L 'http://127.0.0.1:5000/bookings' \
-H 'Content-Type: application/json' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420' \
-d '{
    "start_utc": "2025-07-18T17:48:58.619Z",
    "end_utc": "2025-07-18T17:52:58.619Z"
}'
```

Responding with something like:

```json
{
  "id": "68530559-b54d-4ffe-9f3b-d17bbf141c43",
  "user_id": "105f22e1-6b2a-4b0e-a8a1-0c35309cb420",
  "start_utc": "2025-07-18T17:48:58.619Z",
  "end_utc": "2025-07-18T17:52:58.619Z"
}
```

- Through MSS, get token for submitting a job later e.g.

```shell
curl -L 'http://127.0.0.1:5000/token' \
-H 'Content-Type: application/json' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420' \
--data-raw '{
    "user_id": "105f22e1-6b2a-4b0e-a8a1-0c35309cb420",
    "job_id": "17be2242-ebd1-46c4-b249-8bbe018d321c"
}'
```

Responding with something like: 

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiYWRtaW4iOnRydWUsImlhdCI6MTUxNjIzOTAyMn0.KMUFsIDTnFmyG3nMiGM6H9FNFUROf3wh7SmqJp-QV30",
  "token_type": "bearer"
}
```

- Directly submit jobs using the `access_token` e.g.

```shell
curl -L 'http://127.0.0.1:5000/jobs' \
-H 'Content-Type: application/json' \
-H 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiYWRtaW4iOnRydWUsImlhdCI6MTUxNjIzOTAyMn0.KMUFsIDTnFmyG3nMiGM6H9FNFUROf3wh7SmqJp-QV30' \
-F 'upload_file=@"/Users/johndoe/jobs/job_17be2242-ebd1-46c4-b249-8bbe018d321c.json"'
```

Responding like:

```json
{
  "id": "17be2242-ebd1-46c4-b249-8bbe018d321c",
  "user_id": "105f22e1-6b2a-4b0e-a8a1-0c35309cb420",
  "status": "pending"
  // etc.
}
```

- Open [http://127.0.0.1:9181/](http://127.0.0.1:9181/) in your browser. 
  You should find that the jobs only jobs by the owner (John) of the booking run during the booked slot

- Through MSS, you can also view a given job you submitted.

```shell
curl -L 'http://127.0.0.1:5000/jobs/17be2242-ebd1-46c4-b249-8bbe018d321c' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420'
```

- Through MSS, you can cancel a job you submitted (or if you are admin)

```shell
curl -L 'http://127.0.0.1:5000/jobs/17be2242-ebd1-46c4-b249-8bbe018d321c/cancel' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420' \
--data-raw '{
    "reason": "I just felt like it!"
}'
```

- Through MSS, you can also delete a job you submitted (or if you are admin)

```shell
curl -L -X DELETE 'http://127.0.0.1:5000/jobs/17be2242-ebd1-46c4-b249-8bbe018d321c' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420'
```


- Through MSS, you can also view a list of jobs.

```shell
curl -L 'http://127.0.0.1:5000/jobs?skip=1&limit=10' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420'
```

- Through MSS, you can also view a list of bookings.

```shell
curl -L 'http://127.0.0.1:5000/bookings?skip=1&limit=10' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420'
```

- Through MSS, you can cancel your own booking given that you know the booking ID.

```shell
curl -L -X POST 'http://127.0.0.1:5000/bookings/68530559-b54d-4ffe-9f3b-d17bbf141c43/cancel' \
-H 'x-mss-request-id: 5655b4b8-a589-4b8b-89eb-5204625c141a' \
-H 'x-mss-timestamp: 1754995916.082073' \
-H 'x-mss-signature: MTA1ZjIyZTEtNmIyYS00YjBlLWE4YTEtMGMzNTMwOWNiNDIw' \
-H 'x-mss-user-id: 105f22e1-6b2a-4b0e-a8a1-0c35309cb420'
```


## Basic flow

### Creating a booking

- User sends `Booking` data `(start_utc, end_utc)` timestamp pair.
- If `start_utc` falls in the span of another booking or `end_utc` falls in the span of another booking,
  an error is raised
- Otherwise, the booking is saved to the list of bookings with its unique ID.
- The cached view of the current 24-hour slice of the schedule is invalidated 
  if the new booking's span overlaps with the span of that slice.

_Note: The user must specify the backend they are booking when submitting request through MSS_.

### Deleting a booking

- User requests to delete a Booking of the given ID
- It is popped from the list of bookings.
- If its span overlaps with the span of the cached view of the current 24-hour slice of the schedule,
  the cached view is invalidated.

### Managing normal and slot queues

- Workers associated with the `normal_execution` queue and the `booked_execution` queue 
  run the jobs on the normal and the slot queues respectively.
- The `waitlist` queue is not associated with any worker. 
  It is just a First-In-First-Out (FIFO) data structure from which jobs can be
  popped off and put on an appropriate execution queue.
- Whenever a job is received, the cached 24-hour slice of schedule is checked to get
  the booking that should be running now.   
  If none is found, job is sent to `normal_execution` queue.  
  If one is found, more checks are done to determine if the job is for that booking or not.

### When there is no active booking:

- All jobs received are put into the `preprocessing` queue where they are compiled.
- When a job is compiled, its absolute timings (and thus length) are determined.
- If a job's length is greater than the available time on the `normal_execution` queue 
  before the next time slot, it is put in the `waitlist` queue.  
  _(Note that the pending jobs on the `normal_execution` queue are accounted for also when computing the available time.)_
- Otherwise, it is put on the `normal_execution` queue.

### When a booking is active:

- All jobs received are put into the preprocessing queue where they are compiled.
- When a job is compiled, its absolute timings (and thus length) are determined.
- Any job that does not belong to the user who booked the slot is immediately put in the `waitlist` queue.
- Any job that has an explicit property `force_normal_queue=True`, is immediately put in the `waitlist` queue.
- Otherwise, if a job's length is greater than the time left to the end of the slot,   
  it fails immediately and alerts the user that the job is too long for the slot.  
  _(The reason here is to allow the user to decide if they want to elongate 
  their time slot or just put the job on the normal queue.)_
- If the job's length is less than the available time in the slot, it is put on the `booked_execution` queue.  
  _(Note that the pending jobs on the `booked_execution` queue are accounted 
  for also when computing the available time.)_
- Jobs on the `booked_execution` queue run to completion.
- If the idle time between subsequent jobs on the `booked_execution` queue 
  exceeds `MAX_IDLE_TIME`, the next job from the general `waitlist` queue,
  whose length is shorter than the available time to the **start of the next time slot**, 
  is put on the `booked_execution` queue. 
  This implies that if the booker submitted a job before the booking starts, 
  it will not run automatically when the booking starts. It must wait for all the other jobs that are short enough.

### Immediately after a booking ends:

- All the jobs on the `booked_execution` queue are allowed to run to completion.
- All jobs in the waitlist whose length is less or equal to the available time before 
  the next time slot are moved to the `normal_execution` queue.  
  _(Note that the pending jobs on the execution queue are accounted 
  for also when computing the available time.)_
- The jobs on the `normal_execution` queue start running.


## Constraints

- A user can book only `MAX_SLOTS_PER_DAY` time slots per day. 
- A time slot can not be shorter than `MIN_TIME_SLOT_LENGTH` seconds.
- Each booking is associated with one backend and one user.
- Each booking has a unique ID for easy querying and deletion.

## Opportunities

- Since it is possible to precalculate the length of a job basing on the compiled schedules.
  - We can estimate the time when a submitted job will be run basing on 
    its position in the queue it is in, and the jobs or time slots that   
    must complete before it runs.  
    This can be vital information for users.
- It might be possible to cancel a job using [InstrumentController.stop()](https://quantify-os.org/docs/quantify-scheduler/v0.24.0/autoapi/quantify_scheduler/instrument_coordinator/index.html#quantify_scheduler.instrument_coordinator.InstrumentCoordinator.stop)
  But that probably means we always need to call `InstrumentController.start()` always.  
  - This can be used in our software circuit breakers that stop a job in case something is seriously going wrong.

## Inspiration

Inspiration was obtained from:

- [IBM's Session mode](https://docs.quantum.ibm.com/guides/execution-modes#session-mode)
