# Athena Ticketing System — Summary

## Overview

Athena is the IT service management (ITSM) ticketing system used by the **University of Pennsylvania Health System (UPHS / Penn Medicine)**. It provides a RESTful API for programmatically creating, retrieving, updating, and querying work items. All data is transmitted over HTTPS and returned as JSON.

---

## Work Item Types

Athena manages three primary work item types:

| Type | Prefix | Description |
|------|--------|-------------|
| **Incident** | `IR` | Break/fix requests — something is broken and needs repair (e.g., "My PC is not working") |
| **Service Request** | `SR` | User requests for something to be provided (e.g., "Please grant me access to the team drive") |
| **Change Request** | `CR` | Planned changes to infrastructure or applications, with approval workflows, implementation/test/backout plans, and scheduled downtime windows |

Additional sub-types include **Manual Activities** and **Review Activities**, which are child tasks associated with service requests or change requests.

---

## Authentication

- **Method:** OAuth2 token-based authentication (JWT)
- **Auth Endpoint:** `POST` to the token URL with `content-type: application/x-www-form-urlencoded`
- **Required Fields:** `username`, `password`, `grant_type` (password), `client_id`
- **Token Usage:** Include the JWT in the `Authorization: bearer <token>` header on all subsequent API calls
- **Access:** A `client_id` is required; obtained by submitting a help desk ticket requesting Athena API access
- **Error Codes:**
  - `401 Unauthorized` — invalid or expired token
  - `403 Forbidden` — valid token but insufficient permissions for the resource

### Configured Credentials

| Parameter | Value |
|-----------|-------|
| Client ID | `IAM_rtu5h` |
| Username | `aslanuka` |
| Auth URL | `https://uphsnet.uphs.upenn.edu/athenaapi/oauth2/token` |

---

## API Endpoints

### Base URLs

| Environment | Base URL |
|-------------|----------|
| **Production** | `https://uphsnet.uphs.upenn.edu/athenaapi/` |
| **Test** | `https://uphsnettest2016.uphs.upenn.edu/athenaapi/` |

### Key Endpoint Categories

#### Incidents (`/v1/incident`)
- `POST /v1/incident` — Create an incident
- `PUT /v1/incident` — Update an incident
- `GET /v1/incident/{id}` — Retrieve an incident by ticket ID or entity ID
- `POST /v1/incident/escalate` — Escalate an incident
- `POST /v1/incident/convert/servicerequest` — Convert an incident to a service request
- `POST /v1/incident/ConvertRevertParent` — Convert/revert to parent incident
- `POST /v1/incident/{id}/children` — Add or remove child incidents

#### Service Requests (`/v1/servicerequest`)
- `POST /v1/servicerequest` — Create a service request
- `PUT /v1/servicerequest` — Update a service request
- `POST /v1/servicerequest/pch` — Create a PennChart access request
- `POST /v1/servicerequest/convert/incident` — Convert a service request to an incident
- `POST /v1/servicerequest/convert/jira/{id}` — Convert to a Jira issue
- `POST /v1/servicerequest/activity/complete` — Complete a service request
- `POST /v1/servicerequest/status` — Update service request status

#### Change Requests (`/v1/changerequest`)
- `POST /v1/changerequest` — Create a change request
- `PUT /v1/changerequest` — Update a change request
- `GET /v1/changerequest/{id}` — Retrieve a change request
- `GET /v1/changerequest/search/{id}` — Search change requests
- `POST /v1/changerequest/status` — Update change request status
- `POST /v1/changerequest/revertToDraft/{id}` — Revert to draft status
- `POST /v1/changerequest/pch` — Create/update PennChart change requests

#### Approvals (`/v1/approvals`)
- `GET /v1/approvals` — List pending change request approvals for the logged-in user
- `POST /v1/approvals` — Submit an approval decision (approve/reject with comments)
- `GET /v1/approvals/history` — List completed approvals (default: last 90 days)

#### Generic Object Operations (`/v1/object`)
- `GET /v1/object/{id}` — Retrieve any Athena object by entity ID
- `GET /v1/object/query` — Query objects with simple filter criteria (returns paged results)
- `POST /v1/object/query` — Query using XML criteria across relationships
- `GET /v1/object/history/{id}` — Retrieve change history for an object
- `GET /v1/object/meta` — Get class/type projection definitions
- `GET /v1/object/template` — Get data from an object template
- `POST /v1/object/apply_template` — Apply a template to an object

#### Activities
- `POST /v1/manualactivity` — Create a manual activity
- `PUT /v1/manualactivity` — Update a manual activity
- `GET /v1/manualactivity/{id}` — Retrieve a manual activity
- `GET /v1/reviewactivity/{id}` — Retrieve a review activity
- `PUT /v1/reviewactivity` — Update a review activity

#### Attachments (`/v1/attachments`)
- `POST /v1/attachments/{entityId}` — Add an attachment to a work item
- `GET /v1/attachments/{fileId}` — Retrieve an attachment
- `DELETE /v1/attachments/{fileId}` — Delete an attachment

#### Enums (`/v1/enums`)
- `GET /v1/enums/{enumId}` — Get enum values by ID (flat list)
- `GET /v1/enums/tree/{enumId}` — Get enum values in a hierarchy (tree structure)
- `GET /v1/enums/all` — Retrieve all enumerated list items from cache
- `POST /v1/enums/cache/{enumId}` — Refresh enum cache

#### View/Filter Endpoint
- `POST /v1/view/workitem?type=incident` — Query incidents using advanced JSON filter criteria
- `POST /v1/view/workitem?type=servicerequest` — Query service requests using advanced JSON filter criteria
- `POST /v1/view/workitem?type=changeRequest` — Query change requests using advanced JSON filter criteria

#### Other
- `GET /health` — Health check endpoint
- `POST /v1/canon/ticket/create` — Send a ticket to Canon Printing
- `POST /v1/mypennaccess/refresh_roles` — Refresh myPennAccess roles

---

## Configured API URLs (from .env)

| Purpose | URL |
|---------|-----|
| Base URL | `https://uphsnet.uphs.upenn.edu/athenaapi/` |
| Auth Token | `https://uphsnet.uphs.upenn.edu/athenaapi/oauth2/token` |
| Incident View | `https://uphsnet.uphs.upenn.edu/athenaapi/v1/view/workitem?type=incident` |
| Service Request View | `https://uphsnet.uphs.upenn.edu/athenaapi/v1/view/workitem?type=servicerequest` |
| Incident CRUD | `https://uphsnet.uphs.upenn.edu/athenaapi/v1/incident/` |
| Service Request CRUD | `https://uphsnet.uphs.upenn.edu/athenaapi/v1/servicerequest/` |
| Change Request CRUD | `https://uphsnet.uphs.upenn.edu/athenaapi/v1/changerequest/` |
| IR Support Group Enum (tree) | `https://uphsnet.uphs.upenn.edu/athenaapi/v1/enums/tree/c3264527-a501-029f-6872-31300080b3bf` |
| SR Support Group Enum (tree) | `https://uphsnet.uphs.upenn.edu/athenaapi/v1/enums/tree/23c243f6-9365-d46f-dff2-03826e24d228` |

---

## Querying & Filtering

### Simple Query (`GET /v1/object/query`)
Uses URL query parameters:
- `type` — Class name (e.g., `incident`, `servicerequest`)
- `$filter` — Simple filter expression (e.g., `CreatedDate gt '9-20-2018'`)
- `$orderby` — Sort field and direction (e.g., `CreatedDate Desc`)
- `$top` — Max results to return
- `$skip` — Offset for paging
- `$select` — Comma-separated list of fields to include

### Advanced View Filter (`POST /v1/view/workitem`)
Uses a JSON body with structured filter objects supporting:
- **Conditions:** `and`, `or`
- **Operators:** `eq`, `ne`, `gt`, `lt`, `contains`, `in`, `not in`, `is null`, `like`
- **Dynamic date values:** `[now]`, `[today]`, with offsets like `[now]-1d`, `[today]+7d`
- **Properties:** `Title`, `Priority`, `Status`, `Command_Center`, `ResolvedDate`, `ScheduledStartDate`, `ScheduledEndDate`, `Downtime`, `IsParent`, etc.

### JSON Filter Template (from .env)
```json
[{"condition":"and","filters":[{"condition":"and","property":"name","operator":"eq","value":"{{TICKET_ID}}"}]}]
```
This template is used to look up a specific ticket by its ID (e.g., `IR1959493` or `SR1959584`).

---

## Type Projections

Type Projections control which related entities are included in API responses. They prevent over-fetching by defining which relationships to return.

### Available Projection Helpers

| Name | Use Case |
|------|----------|
| `incidentform` | Full incident with all relationships — use for single incident retrieval |
| `incidentlist` | Incident with affected user, assigned user, and SLA info — use for lists |
| `servicerequestform` | Full service request with all relationships — use for single SR retrieval |
| `servicerequestlist` | Service request with affected user and assigned user — use for lists |
| `manualactivityform` | Full manual activity — use for single activity retrieval |
| `manualactivitylist` | Manual activity with assigned user — use for lists |

### Controlling Response Shape
- `$expand` — Comma-separated list of relationships to include (e.g., `affectedUser,createdByUser`)
- `$select` — Comma-separated list of fields (e.g., `title,description,affectedUser/displayName`)

---

## Key Data Model Fields

### Incident Fields
| Field | Description |
|-------|-------------|
| `entityId` | Unique GUID identifier |
| `id` | Human-readable ticket ID (e.g., `IR1959493`) |
| `title` | Short summary |
| `description` | Detailed description |
| `status` | Enum: Active, Pending, Resolved, Closed, etc. |
| `priority` | Numeric (1 = highest severity) |
| `impact` | Enum (e.g., "Routine Disruption") |
| `urgency` | Enum (e.g., "Medium") |
| `classification` | Categorization enum |
| `supportGroup` | Assigned support team (e.g., "Service Desk") |
| `affectedUser` | The user experiencing the issue |
| `assignedToUser` | The analyst working the ticket |
| `createdByUser` | Who created the ticket |
| `primaryOwner` | Primary owner of the incident |
| `contactMethod` | Phone number or contact info |
| `location` / `floor` / `room` | Physical location details |
| `escalated` | Boolean escalation flag |
| `isParent` | Whether this is a parent incident |
| `command_Center` | Command center enum |
| `createdDate` / `resolvedDate` / `closedDate` | Lifecycle timestamps |
| `actionLogs` / `userComments` / `analystComments` | Communication logs |
| `fileAttachments` | Attached files |
| `relatedWorkItems` | Linked tickets |

### Service Request Fields
Similar to incidents, plus:
- `area` — Service area enum (e.g., "Directory")
- `notes` — Implementation notes
- `completedDate` — When the request was fulfilled
- `activities` — Child manual activities

### Change Request Fields
Includes all standard fields plus:
- `implementationPlan` / `testPlan` / `backoutPlan` — Required plans
- `reason` — Justification for the change
- `risk` / `downtime` — Risk and downtime enums
- `crType` — Change type enum
- `scheduledStartDate` / `scheduledEndDate` — Planned window
- `scheduledDowntimeStartDate` / `scheduledDowntimeEndDate` — Downtime window
- `implementationResults` — Post-implementation status
- `reviewActivities` — Approval workflow activities with reviewers
- `usersToNotify` / `impactedItems` — Notification and impact tracking
- `isEmergencyChange` — Emergency change flag (PennChart changes)
- `isCovid19Related` — COVID-19 flag (PennChart changes)

---

## Enum Values & GUIDs

Many fields use enum data types referenced by GUID or name. You can specify either the `id` (GUID) or `name` when setting values, though using the `id` is more performant.

### Enum Lookup IDs

Each enum type has a unique ID used with the `/v1/enums/tree/{enumId}` endpoint:

| Enum Name | Enum ID | Description |
|-----------|---------|-------------|
| `IncidentStatusEnum` | `89b34802-671e-e422-5e38-7dae9a413ef8` | Incident statuses |
| `ServiceRequestStatusEnum` | `4e0ab24a-0b46-efe6-c7d2-5704d95824c7` | Service request statuses |
| `ChangeStatusEnum` | `0bf0a71b-9e9e-f719-0271-c9a4ff352600` | Change request statuses |
| `ActivityStatusEnum` | `57db4880-000e-20bb-2f9d-fe4e8aca3cf6` | Activity statuses |
| `System.WorkItem.TroubleTicket.ImpactEnum` | `11756265-f18e-e090-eed2-3aa923a4c872` | Impact levels |
| `System.WorkItem.TroubleTicket.UrgencyEnum` | `04b28bfb-8898-9af3-009b-979e58837852` | Incident urgency |
| `ServiceRequestUrgencyEnum` | `eb35f771-8b0a-41aa-18fb-0432dfd957c4` | SR urgency |
| `ServiceRequestPriorityEnum` | `d55e65ea-fae9-f7db-0937-843bfb1367c0` | SR priority |
| `IncidentClassificationEnum` | `1f77f0ce-9e43-340f-1fd5-b11cc36c9cba` | Incident classification (large hierarchy) |
| `IncidentSourceEnum` | `5d59071e-69b3-7ef4-6dee-aacc5b36d898` | Incident source |
| `ServiceRequestSourceEnum` | `848211a2-393a-6ec5-9c97-8e1e0cfebba2` | SR source |
| `IncidentResolutionCategoryEnum` | `72674491-02cb-1d90-a48f-1b269eb83602` | Resolution categories (large hierarchy) |
| `IncidentTierQueuesEnum` | `c3264527-a501-029f-6872-31300080b3bf` | IR support group/tier queues |
| `ServiceRequestSupportGroupEnum` | `23c243f6-9365-d46f-dff2-03826e24d228` | SR support groups |
| `CRSupportGroupList` | `68c15e14-373a-2407-eee8-2ce051ee6a63` | CR support groups |
| `Location` | `31595f15-44d1-58cf-f5b4-03d0f1b1921b` | Location list |
| `Floor` | `bf0bc17f-9091-92bd-912f-6284eb05947c` | Floor list |
| `ServiceRequestAreaEnum` | `3880594c-dc54-9307-93e4-45a18bb0e9e1` | SR area (large hierarchy) |
| `ChangeCategoryEnum` | `ae73def3-8d2f-c2e9-59c8-12864b7c56df` | Change categories |
| `ChangeRiskEnum` | `347a02c1-9784-f335-04b0-662efc8d6676` | Change risk levels |
| `ChangeImpactEnum` | `44edd2ff-6280-afb7-3a0d-d6e8a711d894` | Change impact levels |
| `ChangePriorityEnum` | `b40092af-f163-af28-6150-bb0ffa677660` | Change priority |
| `Change_Type` | `11e09b1e-7897-6706-942b-95c827ef01db` | Change type |
| `ActivityPriorityEnum` | `65a34474-f43d-d880-7eb0-bad49efa7cf1` | Activity priority |
| `ActivityAreaEnum` | `0d1c5836-644e-bfe4-5adf-cfe40fc08dfa` | Activity area |
| `Confirmed_Resolution` | `4c5c7b9c-a5bc-d128-4637-460002846eba` | Confirmed resolution |
| `Yes_No_NA` | `7eede878-7bf0-ac87-770c-45ba4851b528` | Yes/No enum |
| `Command_Center_List` | `98876306-c914-ccd6-50fd-ee05a92a87e0` | Command center items |
| `Specialty` | `1f997c48-1f42-5148-2958-07fdaf53da16` | Specialty list |
| `Increments` | `c30df9e1-26f9-6f16-4928-794aa7fd843a` | Increment values |
| `ChangeAreaEnum` | `28f88c04-d11d-78c0-a237-fa9abd6c6478` | Change area |

### Incident Status Values

| GUID | Label | Notes |
|------|-------|-------|
| `5e2d3932-ca6d-1515-7310-6f58584df73e` | Active | Has children: Pending, Updated by Affected User |
| `b6679968-e84e-96fa-1fec-8cd4ab39c3de` | → Pending | Child of Active |
| `b7ba8903-66a1-485f-4418-00d06abf1235` | → Updated by Affected User | Child of Active |
| `2b8830b6-59f0-f574-9c2a-f4b4682f1681` | Resolved | |
| `bd0ae7c4-3315-2eb3-7933-82dfc482dbaf` | Closed | |
| `9accddda-fbf5-10d4-b402-69bdd276a69b` | Work in Progress | |

### Service Request Status Values

| GUID | Label |
|------|-------|
| `a52fbc7d-0ee3-c630-f820-37eae24d6e9b` | New |
| `72b55e17-1c7d-b34c-53ae-f61f8732e425` | Submitted |
| `59393f48-d85f-fa6d-2ebe-dcff395d7ed1` | In Progress |
| `05306bf5-a6b9-b5ad-326b-ba4e9724bf37` | On Hold |
| `b026fdfd-89bd-490b-e1fd-a599c78d440f` | Completed |
| `21dbfcb4-05f3-fcc0-a58e-a9c48cde3b0e` | Failed |
| `674e87e4-a58e-eab0-9a05-b48881de784c` | Cancelled |
| `c7b65747-f99e-c108-1e17-3c1062138fc4` | Closed |

### Change Request Status Values

| GUID | Label |
|------|-------|
| `a87c003e-8c19-a25f-f8b2-151b56670e5c` | New |
| `504f294c-ae38-2a65-f395-bff4f085698b` | Submitted |
| `6d6c64dd-07ac-aaf5-f812-6a7cceb5154d` | In Progress |
| `dd6b0870-bcea-1520-993d-9f1337e39d4d` | On Hold |
| `68277330-a0d3-cfdd-298d-d5c31d1d126f` | Completed |
| `85f00ead-2603-6c68-dfec-531c83bf900f` | Failed |
| `877defb6-0d21-7d19-89d5-a1107d621270` | Cancelled |
| `f228d50b-2b5a-010f-b1a4-5c7d95703a9b` | Closed |
| `13fcec71-3230-f6ac-0a73-9b261c57d6c9` | Draft |
| `b5e8b30d-c621-f989-05a6-421ba37353af` | Pending Approval |

### Activity Status Values

| GUID | Label |
|------|-------|
| `50c667cf-84e5-97f8-f6f8-d8acd99f181c` | Pending |
| `11fc3cef-15e5-bca4-dee0-9c1155ec8d83` | In Progress |
| `d544258f-24da-1cf3-c230-b057aaa66bed` | On Hold |
| `9de908a1-d8f1-477e-c6a2-62697042b8d9` | Completed |
| `144bcd52-a710-2778-2a6e-c62e0c8aae74` | Failed |
| `89465302-2a23-d2b6-6906-74f03d9b7b41` | Cancelled |
| `baa948b5-cc6a-57d7-4b56-d2012721b2e5` | Rerun |
| `eaec5899-b13c-d107-3e1a-955da6bf9fa7` | Skipped |
| `a4623c79-fa89-b7d0-e506-c1e44dba04e5` | Active |

### Impact Values (Incidents)

| GUID | Label |
|------|-------|
| `8f1a713e-53aa-9d8a-31b9-a9540074f305` | Routine Disruption |
| `80cc222b-2653-2f68-8cee-3a7dd3b723c1` | Local Outage |
| `d2b5e816-2d24-8e7d-a61f-2cceaeac2664` | Enterprise Outage |

### Urgency Values (Incidents)

| GUID | Label |
|------|-------|
| `725a4cad-088c-4f55-a845-000db8872e01` | Medium |
| `02625c30-08c6-4181-b2ed-222fa473280e` | High |
| `2f8f0747-b6cb-7996-fd4a-84d09743f218` | Urgent |

### Urgency Values (Service Requests)

| GUID | Label |
|------|-------|
| `b02d9277-a9fe-86f1-e95e-0ba8cd4fd075` | Low |
| `c3945d25-5f43-36c4-c1c9-2d6da1912d07` | Medium |
| `530ee945-9d39-8801-52aa-b910694e0254` | High |
| `cf01467e-5f6d-a521-867b-7ab453261171` | Immediate |

### Priority Values (Service Requests)

| GUID | Label |
|------|-------|
| `1e070214-693f-4a19-82bb-b88ee6362d98` | Low |
| `dd43a3a8-c640-2146-85a4-77978e3bb375` | Medium |
| `536beaf3-62a8-5dd0-248a-39c2bf86d3bc` | High |
| `d0a0fadd-7f17-c0a2-cb2f-00e15c51282c` | Immediate |

### Change Category Values

| GUID | Label |
|------|-------|
| `02d2f92f-d925-5ad6-eb1f-67020701697a` | Standard |
| `c0126730-ab4a-4c62-26e0-7706bc176413` | Minor |
| `e9ee7044-3a42-a34b-1237-ec0d32f2377a` | Major |
| `357662a7-451c-df62-ed68-3147bdff324e` | Emergency |

### Change Type Values

| GUID | Label |
|------|-------|
| `aba0be0b-84d3-77f8-8480-c70604649404` | Standard |
| `db176baf-5d81-b44f-c964-17422c1f09af` | Emergency |
| `5732b775-cac7-2538-94f7-ec99b333dec5` | PennChart Emergency |
| `1ccd1583-bb9e-5611-f11b-645f13fae610` | PennChart |
| `32fb84f6-98e8-5e92-0027-ac5d87fe33e2` | LGH Standard |
| `e93dcd6c-e266-787f-8c1a-4ffda2bc81d5` | LGH Emergency |
| `5e4df245-d6c9-7660-42e5-5df2fad80e21` | LGH eHealth |
| `dcc83008-b946-6e7a-3c5d-f7d5bc569f35` | LGH eHealth Emergency |

### Change Risk Values

| GUID | Label |
|------|-------|
| `13b87263-844c-833f-2fae-30939de58244` | Low |
| `d92ca060-fd52-27dc-8268-9b0f5fd4ffda` | Medium |
| `978b7e07-10c5-25c1-e6f4-e1df0579fc82` | High |

### Change Impact Values

| GUID | Label |
|------|-------|
| `312b4612-6b0e-63e9-e9c4-0a4d04d7363a` | Low |
| `70ca6737-6f4d-ea78-c392-2cfc61eaadfc` | Medium |
| `e90e735e-959b-eb5b-41be-1fd07c30a740` | High |

### Confirmed Resolution Values

| GUID | Label |
|------|-------|
| `24383eb0-5cf8-a132-df65-6f98fbc68a7f` | Yes |
| `280c12ea-7518-805a-d383-0bbaf34190fb` | No |

### Entity Type GUIDs

| GUID | Type |
|------|------|
| `a604b942-4c7b-2fb2-28dc-61dc6f465c68` | System.WorkItem.Incident |
| `04b69835-6343-4de2-4b19-6be08c612989` | System.WorkItem.ServiceRequest |

### Command Center List (Active Items)

| GUID | Label |
|------|-------|
| `2a002c62-16a4-f441-8213-6e5f2725a535` | PPMC Wright Saunders 2 Prep/Recovery |
| `86da5397-053e-38fe-fc95-439009fa2f0b` | HUP Cedar/HUP Ravdin 2&7 Label Printing |
| `89b08134-bc0d-262c-d570-b55cf24c6690` | Aramark Command Center |
| `be405cce-e60b-e97a-4b4c-fed5b2f30e8e` | PennChart March 2026 Update |
| `2d3e6286-c785-2763-595c-2e1c73ed446d` | AS-OBGYN Triggering Interface |
| `7e7b7d79-4d5f-614b-6591-ab0141e68f8b` | Riskonnect (SafetyNet) |
| `03f41423-3f7a-6058-abad-4ab9ca39762c` | Penn Primary Care Walnut Street |
| `59941a0b-ad0c-0a87-a4b1-d5f2d023cf06` | Beacon Refuel Project 2026 |
| `405c9e6e-cb97-a1d7-d147-6e51c5b0fe5e` | Rittenhouse PPID & Lab Label Printing |
| `79f38847-21db-c016-2872-dfbde185f9f1` | Hospital at Home Implementation |
| `a8bc188b-d4df-9ec7-1aef-cb01d79aacc4` | Patient Accounting PC Refresh |

---

## Support Group Hierarchy

### ⚠️ CRITICAL: IR and SR Support Groups Use Different GUIDs

**Incident (IR) and Service Request (SR) support groups have the same group names but completely different GUIDs.** There are 0 shared GUIDs between the two enum lists. When assigning a support group, you must use the correct GUID for the ticket type:

- **IR support groups** → Use enum `IncidentTierQueuesEnum` (`c3264527-a501-029f-6872-31300080b3bf`)
- **SR support groups** → Use enum `ServiceRequestSupportGroupEnum` (`23c243f6-9365-d46f-dff2-03826e24d228`)
- **CR support groups** → Use enum `CRSupportGroupList` (`68c15e14-373a-2407-eee8-2ce051ee6a63`)

### Top-Level IR Support Groups (Incident Tier Queues)

| Group | IR GUID |
|-------|---------|
| Application Development | `19937f4a-082a-8d0d-a525-cb9d722c1f63` |
| Applications | `4f8bfaca-a980-27c5-11e7-957b0bbff24b` |
| DXC | `a8a8077d-f85a-632f-31d6-c4c82078a597` |
| EUS | `ae9eb3ff-458a-206f-7815-129d50efa285` |
| Integration | `3f199b4b-226e-5ab4-f5f9-c1356e3520bc` |
| IS Education | `373c783d-67f6-9b79-d3e6-5e9e977f47bf` |
| IS Operations | `f6f5d0e0-d01d-8f10-0bfe-73a7efd77315` |
| LGH | `739cb952-6dac-7e6d-69d1-f300567cc352` |
| Non-Corp IS | `0fee5880-76d3-522d-fbd8-f921dfd05d43` |
| PennChart | `ab139906-1ce2-7115-9124-5bc600369550` |
| PennChart Interfaces (Bridges) | `a9921cf2-38ff-7fd8-6b97-513cf8ba9ff2` |
| PennDnA | `41148f0a-e63f-5c27-8d51-36617f2789ca` |
| PERC | `81a63c32-9944-8b3c-593f-952cd845da8d` |
| PMDH Dispatch | `041a614f-f77b-bbf8-73ab-692eae317311` |
| Service Desk | `ec749166-07c5-eba6-35ba-bd32fa8ed7d2` |
| Technology\Infrastructure | `17326dc5-8e2f-bc10-1085-85bb81fee7db` |

### Top-Level SR Support Groups

| Group | SR GUID |
|-------|---------|
| Application Development | `0e6bd2a8-d842-8c8f-1eca-8ea5a9d5610b` |
| Applications | `3f470d2d-ecaa-f127-7a59-c08d2976e47c` |
| DXC | `cd097ec1-9c26-1290-4e48-48cc17c8e4da` |
| EUS | `bea228bb-7633-b24b-9295-099f61afc92c` |
| Integration | `89daa914-50bc-76d1-765f-cee1bf5bf9d9` |
| IS Education | `6d725e40-affd-7bf2-8b61-2fb0cf537087` |
| IS Operations | `d1942c36-a528-28da-0a53-f363cf452442` |
| LGH | `80e77aba-a0ef-baa3-e26c-00e44476d2e1` |
| Non-Corp IS | `4fe35a8b-9611-cb9f-0220-cc43328a29ee` |
| PennChart | `1d0a10db-b98b-67ce-3a61-e7d6d90e558b` |
| PennChart Interfaces (Bridges) | `483b3fde-b0f4-754e-fed0-3a33a1ee8491` |
| PennDnA | `dc386d29-9f8f-8adb-7659-44b5d3484033` |
| PERC | `16154f59-83ff-5d5b-8109-3f741d4f5e23` |
| PMDH Dispatch | `ca50f875-1628-eba5-3aa4-ca312ae97bfc` |
| Service Desk | `043871eb-f69c-2330-7cbb-155b04fe24ea` |
| Technology\Infrastructure | `ae95e2a2-b456-f09e-20e0-491f9d5461e9` |

### Commonly Used Sub-Groups (IR GUIDs → SR GUIDs)

| Sub-Group | IR GUID | SR GUID |
|-----------|---------|---------|
| Service Desk | `ec749166-07c5-eba6-35ba-bd32fa8ed7d2` | `043871eb-f69c-2330-7cbb-155b04fe24ea` |
| Service Desk\Validation | `1a59b3b9-84a3-13ce-f50c-79b8a99f5531` | `c954d465-65a0-9e43-9b02-b353e87bdb37` |
| Service Desk\Service Desk - Epic | `8e706a5a-a6e6-7a9e-a308-481846a8dce1` | `5a937757-7e0a-c22d-7967-05202a369b87` |
| Service Desk\ATLAS | `b4b69f54-24f4-0a43-83f4-d3d7dccc8440` | `dbfd82aa-f0a7-56b9-d0c5-4ba1e0e1ebc4` |
| PennChart | `ab139906-1ce2-7115-9124-5bc600369550` | `1d0a10db-b98b-67ce-3a61-e7d6d90e558b` |
| PennChart\ED | `72bac846-40cc-8c58-c749-30e2a59cdde5` | `cf8549ba-5827-d36d-76a0-e54dd90b06ac` |
| PennChart\User Provisioning | `221e658a-8205-8f9b-683b-48c07b26a981` | `2624881f-0303-7e3d-28e1-7fa1fb7f1198` |
| PennChart\Secure Chat | `702ac4bb-e659-519a-cf7c-aeda8e273ecb` | `f7a765c7-ba0b-d431-6ad1-08de5f31dca5` |
| IS Education | `373c783d-67f6-9b79-d3e6-5e9e977f47bf` | `6d725e40-affd-7bf2-8b61-2fb0cf537087` |
| IS Operations\ISAAC | `45e58883-7836-24da-b837-6e9fded20512` | `974ae343-f738-5c5c-efc2-477c09b061b2` |
| IS Operations\Athena | `e8b59269-964e-1e2a-d9f0-da27054d4e5e` | `c3e095f6-f80d-3dda-7732-6d81266f5760` |

Notable sub-groups under the top-level groups include teams for PennChart (Ambulatory, Inpatient, Pharmacy, Revenue Cycle, ED, Secure Chat, etc.), EUS campus locations (HUP, HUP Cedar, HUP West, PPMC, PAH, CCH, MCP, RITT, PMUC, Campus, Central Imaging, etc.), Technology/Infrastructure (Cybersecurity, Database Services, Networking, Telecom, Messaging, Domain Services, CyberArk/PAM, etc.), LGH (Epic, Clinical Applications, Technical Services, Shared Services, HIM Team, Non-Corp IS), and many more. The full hierarchical lists contain 324 IR groups and 326 SR groups.

---

## Location & Floor Hierarchy

Tickets in Athena reference physical locations within Penn Medicine facilities via the `location`, `floor`, and `room` fields. Locations and floors are enum values with GUIDs.

### Enum IDs

| Enum | Enum ID | Description |
|------|---------|-------------|
| `Location` | `31595f15-44d1-58cf-f5b4-03d0f1b1921b` | Hierarchical location list (facilities → buildings/sites) |
| `Floor` | `bf0bc17f-9091-92bd-912f-6284eb05947c` | Flat floor list (1st–26th + special floors) |

### Location Statistics

- **Total location entries:** 471 (including sub-locations)
- **Top-level locations:** 19
- **Max depth:** 2 (e.g., `PPMC\PAC\TSICU`)
- **All entries are active** (none disabled)

### Top-Level Locations

| Location | GUID | Children | Description |
|----------|------|----------|-------------|
| **CAMPUS** | `47ecf1f0-d95b-edae-0b43-ea45123dad56` | 36 | University City campus buildings (Abramson, BRB, CRB, Goddard, Vagelos, etc.) |
| **CCH** | `87321e2e-ca8f-2585-0936-0781f7ee0368` | 37 | Chester County Hospital and satellite sites (Exton, Kennett Square, West Chester, West Grove) |
| **Doylestown (PMDH)** | `40fc913d-14f3-01d8-0378-128a62fd14c7` | 5 | Penn Medicine Doylestown Hospital (Main Hospital, Pavilion, Physician Offices, Warrington) |
| **HUP** | `d5469f7c-d8b1-ff41-255a-9956ea42d843` | 22 | Hospital of the University of Pennsylvania (Founders, Gates, Maloney, Ravdin, Silverstein, White, etc.) |
| **HUP Cedar** | `1048d461-bf87-1657-3f20-c33d9ac2562b` | 1 | HUP Cedar Avenue campus (54th & Cedar Ave) |
| **HUP Pavilion** | `069e0889-cce3-3b74-95b6-3ce4f1a9bd78` | 3 | HUP Pavilion (Campus, Center, City wings) |
| **LGH** | `2804274b-7cc4-be8a-cc19-a527f68d9dce` | 19 | Lancaster General Health (Main Hospital, Emergency Dept, DOP, SOP, Women & Babies, etc.) |
| **LGHP** | `3705512f-27f1-cd16-d514-2597d99910bf` | 80 | Lancaster General Health Physicians (80 practice locations across Lancaster County) |
| **PAH** | `430c52ab-a188-3dd6-c249-b56cf764ca77` | 21 | Pennsylvania Hospital (Ayers, Cathcart, Duncan, Hall Mercer, Preston, Spruce Building, etc.) |
| **PCAM** | `f66abf71-55e8-345c-5f8c-f378864900c6` | 8 | Perelman Center for Advanced Medicine (Atrium, Smilow, Jordan, Pavilions) |
| **PMaH** | `2433006f-2483-fbc6-e988-ef63a43fa124` | 3 | Penn Medicine at Home (Bala Cynwyd, King of Prussia, West Chester) |
| **PPMC** | `11c9a7ba-555d-b4ee-aba2-bb8b78319f5d` | 18 | Penn Presbyterian Medical Center (Cupp, MAB, Mutch, Myrin, PAC with sub-units, Scheie, Wright-Saunders) |
| **PMUC** | `5a724fd5-d145-9562-2a5a-397517299a7c` | 1 | Penn Medicine University City (3737 Market St) |
| **Princeton (MCP)** | `e9a6a516-b05e-939f-c139-feb0f8aa20d1` | 64 | Medical Center Princeton (Main Campus, 60+ practice/clinic locations across NJ) |
| **RITT** | `650b22b2-34b7-6023-1a18-2dee8f82674c` | 4 | Rittenhouse (Main Building 1800 Lombard, Tuttleman Center, PennSTAR ALS, Puentes) |
| **Remote sites (RSI)** | `6f118f43-3110-7b8f-5ccc-ff8a4ee77a9b` | 71 | Remote site installations across PA, NJ, DE (71 satellite clinics, offices, and specialty sites) |
| **Community Connect** | `7d7d0660-9e20-f230-2ffb-95cede49f7e6` | 56 | Community Connect partner practices (independent practices using Penn Medicine systems) |
| **Data Center** | `ae0fc391-bb61-eb69-f177-8acafd095e0b` | 4 | Data centers (Brownstown, Duke Street, Newark, Philadelphia Tier Point) |
| **Remote User** | `30b05c74-7b8c-df07-71ac-95ecf58200d4` | 0 | Remote/work-from-home users |

### Key Hospital Building GUIDs

| Building | Location Path | GUID |
|----------|--------------|------|
| HUP Founders | `HUP\FOUNDERS` | `837fc4d9-5ed3-9288-90f7-0d12ad55a624` |
| HUP Ravdin | `HUP\RAVDIN` | `adb50fb4-5e17-c8f6-586b-cf47f08868f1` |
| HUP Silverstein | `HUP\SILVERSTEIN` | `a3868287-2567-f909-6593-71402062e027` |
| HUP Gates | `HUP\GATES` | `0ab54beb-862f-8bbe-b55c-b0506350bb4a` |
| HUP Maloney | `HUP\MALONEY` | `3164b339-3c3b-013e-acfa-3d918bde0ead` |
| HUP White | `HUP\WHITE` | `b94af944-4361-2fd7-4a9a-70405af2f6b6` |
| HUP Dulles | `HUP\DULLES` | `55733141-4063-b79d-7385-267b66b954ad` |
| HUP Donner | `HUP\DONNER` | `abdf1d00-5447-23b6-027b-2b85cdf0c602` |
| HUP Rhoads | `HUP\RHOADS` | `58f39868-4f5b-c63c-9c28-b13a999583f9` |
| HUP Cedar | `HUP Cedar\HUP Cedar (54th & Cedar Ave)` | `6e7d314f-5936-0d35-323e-48dc157a16bb` |
| PAH Preston | `PAH\PRESTON` | `ecfd513f-8be8-3232-cff8-dd2ede246f33` |
| PAH Duncan North | `PAH\Duncan Building North (700 Spruce St.)` | `a85a4486-af08-9e3e-a381-cfaeeff84f8d` |
| PPMC Wright-Saunders | `PPMC\WRIGHT-SAUNDERS` | `272d4d79-5a79-4c92-7f02-4bc274ee0a2c` |
| PPMC MAB | `PPMC\MAB (3801 Market Street)` | `048e1cce-a709-c236-63e9-f94ce94aa284` |
| PCAM Smilow | `PCAM\SMILOW` | `491b0db4-035c-8432-9190-46ecc2fc1430` |
| LGH Main | `LGH\Lancaster General Hospital` | `af1de3d5-e76a-0084-87f7-78de88356db5` |
| CCH Main | `CCH\CCH Main Building (701 E. Marshall St)` | `5501ae57-e326-a347-2e40-eee1c4414d38` |

### Floor Values

Floors are a **flat list** (no hierarchy) with 37 entries. All are active.

| Floor | GUID |
|-------|------|
| 1st | `3a502c37-4ab6-2587-2142-51a2fec6554e` |
| 2nd | `c970e0ad-8f7a-fa67-66b5-6c12bce9b3f6` |
| 3rd | `a9460c6d-86f2-a592-ca9e-763e297f5f78` |
| 4th | `8a645246-3163-9f8e-969d-4aff11c64a16` |
| 5th | `4fc68bb7-d656-51a0-1ea7-e1357c12695a` |
| 6th | `08eaab31-c236-ade9-95d9-48dc6b424444` |
| 7th | `e6d49014-9b2f-9374-7c19-80dbdcaac62d` |
| 8th | `201d2cc4-56b9-f07d-960c-262bfdaa1992` |
| 9th | `42d47236-0bdf-b81a-4f5b-75f1a9256b6b` |
| 10th | `d3a5b012-90a0-1627-496c-88fdcc4d4571` |
| 11th | `86b3ec3b-7537-09a5-3c0a-fab377777390` |
| 12th | `aca9d686-5792-f947-1521-23a6c7cd14db` |
| 13th | `45617c85-22b7-4265-aa03-f0ac370495b6` |
| 14th | `40f8e8b2-6626-0f0a-9354-5bd27b5c460b` |
| 15th | `4ed0e0e2-d924-8cd3-1554-db9d81f020fd` |
| 16th | `2129eed0-0609-a276-6e4e-ecfa60481bc7` |
| 17th | `6b77d69d-92a9-4e0f-c268-2be7df99d840` |
| 18th | `e6355b79-35d7-03c9-3ced-4df25308c930` |
| 19th | `d4c8601a-6c6d-55d0-d7dd-a3d6911e99fb` |
| 20th | `7fd1c604-03da-69cb-914d-5ca42b0aef74` |
| 21st | `0ac45c41-7a80-61c3-4a62-013a3cd69292` |
| 22nd | `b0329aea-b27c-38f4-f4ac-b1fe1060f2f9` |
| 23rd | `462fbadf-054a-88ae-6bec-34c41e6207c9` |
| 24th | `b502035f-2aff-1434-7bd4-95e70b6b8b71` |
| 25th | `79840988-8710-db7b-d103-991db59f1988` |
| 26th | `7338fa7f-aa86-7fb8-051d-2c1ecc428822` |
| BASEMENT | `fa1c103b-029d-5cef-7499-3985f42c5458` |
| BRIDGE | `67b42111-485c-fb5b-177d-c0dd4775e10b` |
| CONCOURSE | `2f753a42-aec4-72d7-87dd-5438d0a77104` |
| GROUND | `fbfb90b1-29d3-3e35-1f6b-fcdef174d446` |
| LOBBY | `5a15485c-f91b-7436-6939-ad776da12d7d` |
| LOWER LOBBY | `f76ec7a5-7c31-49b7-49e5-5a73865ea23a` |
| MEZZANINE | `505a53be-acb2-8d7e-19ea-e82a16757a5c` |
| PENTHOUSE | `44a46c7a-0c5a-362f-9684-74b6c305f1d8` |
| SUB-BASEMENT | `ff4ca55e-4e48-a29c-2611-e67cb2473856` |
| UPPER MEZZANINE | `8709a385-13c1-21ef-8352-8f0582f4ad59` |
| N/A | `c63ed5ef-512a-d378-fbcb-8811b1525702` |

### Location Abbreviation Reference

| Abbreviation | Full Name |
|-------------|-----------|
| HUP | Hospital of the University of Pennsylvania |
| PAH | Pennsylvania Hospital |
| PPMC | Penn Presbyterian Medical Center |
| PCAM | Perelman Center for Advanced Medicine |
| CCH | Chester County Hospital |
| LGH | Lancaster General Health |
| LGHP | Lancaster General Health Physicians |
| PMDH | Penn Medicine Doylestown Hospital |
| MCP | Medical Center Princeton |
| RITT | Rittenhouse |
| PMUC | Penn Medicine University City |
| PMaH | Penn Medicine at Home |
| RSI | Remote Site Installation |
| BRB | Biomedical Research Building |
| CRB | Clinical Research Building |
| TRL | Translational Research Laboratories |
| FMC | Family Medicine Center |
| DOP | Downtown Outpatient Pavilion (LGH) |
| SOP | Suburban Outpatient Pavilion (LGH) |
| WBH | Women & Babies Hospital (LGH) |
| ABBCI | Ann B. Barshinger Cancer Institute (LGH) |
| MAB | Medical Arts Building (PPMC) |
| PAC | Perelman Ambulatory Care (PPMC) |

---

## User References

When specifying users in API calls (e.g., `affectedUser`, `assignedToUser`), you can reference them by:
- `userName` — Active Directory username (e.g., `"jonestim"`)
- `entityId` — The user's GUID in Athena

The system will look up the first matching user. If no match is found, the request fails.

---

## Swagger Documentation

Interactive API documentation is available at:
- **Test:** `https://uphsnettest2016.uphs.upenn.edu/athenaapi/swagger/docs/v1`

---

## Integration Notes

- Incidents can be converted to service requests and vice versa.
- Service requests can be converted to Jira issues.
- Change requests support a full approval workflow with review activities and reviewer voting.
- The system supports parent-child incident relationships for major incident management.
- Canon Printing integration is available for routing print-related tickets.

---

## Databricks Integration

This project integrates with **Databricks** on Azure for AI/ML capabilities including LLM inference, text embedding generation, and vector-based data storage via a SQL warehouse. Databricks serves as the AI backend for semantic search, ticket similarity analysis, and knowledge base retrieval.

---

### Databricks Connection

| Parameter | Value |
|-----------|-------|
| Server Hostname | `adb-764791440085816.16.azuredatabricks.net` |
| HTTP Path (SQL Warehouse) | `/sql/1.0/warehouses/e518b5f5ef7523fe` |
| Authentication | Personal Access Token (PAT) via `DATABRICKS_API_KEY` |

### Configured Databricks URLs (from .env)

| Purpose | URL |
|---------|-----|
| Claude Sonnet 4.5 (LLM) | `https://adb-764791440085816.16.azuredatabricks.net/serving-endpoints/databricks-claude-sonnet-4-5/invocations` |
| GTE-Large-EN (Embeddings) | `https://adb-764791440085816.16.azuredatabricks.net/serving-endpoints/databricks-gte-large-en/invocations` |
| SQL Warehouse | Server: `adb-764791440085816.16.azuredatabricks.net`, HTTP Path: `/sql/1.0/warehouses/e518b5f5ef7523fe` |

---

### Serving Endpoints

Databricks provides model serving endpoints accessible via REST API. Authentication uses a Bearer token (`DATABRICKS_API_KEY`).

#### Available Serving Endpoints

| Endpoint Name | Status | Category |
|---------------|--------|----------|
| `databricks-claude-opus-4-6` | READY | LLM |
| `databricks-claude-opus-4-5` | READY | LLM |
| `databricks-claude-opus-4-1` | READY | LLM |
| `databricks-claude-sonnet-4-6` | READY | LLM |
| `databricks-claude-sonnet-4-5` | READY | LLM (configured) |
| `databricks-claude-sonnet-4` | READY | LLM |
| `databricks-claude-3-7-sonnet` | READY | LLM |
| `databricks-claude-haiku-4-5` | READY | LLM |
| `databricks-gpt-oss-120b` | READY | LLM |
| `databricks-gpt-oss-20b` | READY | LLM |
| `databricks-llama-4-maverick` | READY | LLM |
| `databricks-gemma-3-12b` | READY | LLM |
| `databricks-meta-llama-3-3-70b-instruct` | READY | LLM |
| `databricks-meta-llama-3-1-8b-instruct` | READY | LLM |
| `databricks-meta-llama-3.1-405b-instruct` | READY | LLM |
| `databricks-gte-large-en` | READY | Embedding (configured) |
| `databricks-bge-large-en` | READY | Embedding |

#### LLM Endpoint — Claude Sonnet 4.5

- **Endpoint:** `POST` to the invocations URL
- **Model ID:** `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
- **Request Format:** OpenAI-compatible chat completions
- **Headers:** `Authorization: Bearer <DATABRICKS_API_KEY>`, `Content-Type: application/json`

**Request Body:**
```json
{
  "messages": [
    {"role": "user", "content": "Your prompt here"}
  ],
  "max_tokens": 100
}
```

**Response Format:**
```json
{
  "model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
  "choices": [
    {
      "message": {"role": "assistant", "content": "Response text"},
      "index": 0,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 37,
    "total_tokens": 57
  },
  "object": "chat.completion",
  "id": "msg_bdrk_...",
  "created": 1776024772
}
```

#### Embedding Endpoint — GTE-Large-EN

- **Endpoint:** `POST` to the invocations URL
- **Model ID:** `gte-large-en-v1.5`
- **Embedding Dimensions:** 1024
- **Supports Batch Input:** Yes (multiple texts in a single request)
- **Headers:** `Authorization: Bearer <DATABRICKS_API_KEY>`, `Content-Type: application/json`

**Request Body:**
```json
{
  "input": ["Text to embed", "Another text to embed"]
}
```

**Response Format:**
```json
{
  "id": "uuid",
  "object": "list",
  "model": "gte-large-en-v1.5",
  "data": [
    {"index": 0, "object": "embedding", "embedding": [0.044, -1.34, ...]},
    {"index": 1, "object": "embedding", "embedding": [0.012, -0.98, ...]}
  ],
  "usage": {"prompt_tokens": 12, "total_tokens": 12}
}
```

---

### SQL Warehouse & Unity Catalog

Data is accessed via the Databricks SQL Warehouse using the `databricks-sql-connector` Python package. The warehouse connects through Unity Catalog with a three-level namespace: `catalog.schema.table`.

#### Connection Method
```python
from databricks import sql as databricks_sql

connection = databricks_sql.connect(
    server_hostname="adb-764791440085816.16.azuredatabricks.net",
    http_path="/sql/1.0/warehouses/e518b5f5ef7523fe",
    access_token=DATABRICKS_API_KEY
)
```

#### Available Catalogs

The workspace contains 20 catalogs. The project uses the `scratchpad` catalog:

| Catalog | Description |
|---------|-------------|
| `scratchpad` | Project workspace (contains `aslanuka` schema) |
| `billing` | Billing data |
| `curated` | Curated datasets |
| `prepared` | Prepared datasets |
| `source_sys` | Source system data |
| `isoperations_analytics` | IS Operations analytics |
| `samples` | Sample datasets |
| `system` | System catalog |
| *(and others)* | Various department-specific catalogs |

#### Project Schema: `scratchpad.aslanuka`

Contains 2 tables used by this project:

---

### Table: `scratchpad.aslanuka.onenote_documentation`

**Purpose:** Stores extracted content from OneNote service desk knowledge base documents with pre-computed vector embeddings. Used for semantic search to find relevant troubleshooting documentation when handling tickets.

| Column | Type | Description |
|--------|------|-------------|
| `content` | `string` | The full text content extracted from a OneNote page |
| `embeddings` | `array<double>` | 1024-dimensional vector embedding of the content (GTE-Large-EN) |
| `notebook` | `string` | Source notebook name (`uphs_notebook` or `lgh_notebook`) |
| `section` | `string` | Section within the notebook (e.g., "Helpdesk Printer Issues", "Epic Ambulatory") |
| `title` | `string` | Page title within the section |

**Statistics:**
- **Total rows:** 6,709
- **Notebooks:** 2 (`uphs_notebook`, `lgh_notebook`)
- **Sections:** 301 distinct sections across both notebooks
- **Embedding dimensions:** 1024

**Notebook Breakdown:**

| Notebook | Description |
|----------|-------------|
| `uphs_notebook` | UPHS (University of Pennsylvania Health System) service desk documentation |
| `lgh_notebook` | LGH (Lancaster General Health) service desk documentation |

**Sample Sections (LGH):** Admin Finance Apps, Athena, Epic Ambulatory, Epic Beacon, Epic ClinDoc Stork ASAP, Epic MyLGHealth, Helpdesk Access Issues, Helpdesk Printer Issues, Helpdesk Tech Support, Networking, VPN, and many more.

**Sample Sections (UPHS):** Merge Eye Care Pacs, Middlewares, PennChart, Active Directory, Citrix, VPN, Printing, and many more.

**Content Format:** Each entry contains structured troubleshooting documentation typically including: Problem Description, Cause, Information Gathering steps, Resolution steps, Escalation paths, and Support Group assignments.

---

### Table: `scratchpad.aslanuka.ir_embeddings`

**Purpose:** Stores pre-computed vector embeddings for incident tickets. Used for ticket similarity search — given a new ticket, find historically similar incidents to assist with resolution.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `string` | Ticket identifier (mostly `IR`-prefixed Athena incident IDs, e.g., `IR1959493`) |
| `ticket_embedding` | `array<double>` | 1024-dimensional vector embedding of the ticket content (GTE-Large-EN) |

**Statistics:**
- **Total rows:** 42,146
- **Embedding dimensions:** 1024
- **ID distribution:** ~98% are `IR`-prefixed (41,290 tickets), with a small number of numeric-only IDs (856 tickets, likely legacy or imported records)

---

### Semantic Search Pattern

The tables support a **Retrieval-Augmented Generation (RAG)** workflow:

1. **User describes an issue** → Text is sent to the GTE-Large-EN embedding endpoint to generate a 1024-dimensional vector
2. **Documentation search** → The vector is compared against `onenote_documentation.embeddings` using cosine similarity to find relevant knowledge base articles
3. **Ticket similarity search** → The vector is compared against `ir_embeddings.ticket_embedding` to find historically similar incidents
4. **LLM-powered response** → Retrieved context (documentation + similar tickets) is passed to Claude Sonnet 4.5 to generate a recommended resolution

This enables AI-assisted ticket handling where the system can suggest resolutions based on both institutional knowledge (OneNote docs) and historical ticket patterns.

---

## MCP Tools for Development

This project uses several **Model Context Protocol (MCP)** servers to assist with development. These tools are available in the IDE (VS Code with Cline) and provide capabilities that accelerate implementation, maintain cross-session context, and enable browser-based testing.

### Connected MCP Servers

#### 1. Context7 (`github.com/upstash/context7-mcp`)

**Purpose:** Query up-to-date documentation for any programming library or framework.

**Tools:**
- `resolve-library-id` — Resolve a package name to a Context7-compatible library ID
- `query-docs` — Retrieve current documentation and code examples for a library

**Use in this project:**
- Query FastAPI docs for current patterns (async endpoints, dependency injection, WebSocket handlers)
- Query Pydantic v2 docs for model syntax, validators, and settings management
- Query httpx docs for async HTTP client patterns
- Query pytest / pytest-asyncio docs for testing patterns
- Query databricks-sql-connector docs for SQL warehouse usage
- Ensures code uses current API patterns rather than outdated training data

**Example workflow:**
1. `resolve-library-id` with query "FastAPI WebSocket" → get library ID
2. `query-docs` with that ID → get current WebSocket implementation examples

---

#### 2. Filesystem Server (`github.com/modelcontextprotocol/servers/tree/main/src/filesystem`)

**Purpose:** Enhanced file system operations beyond basic read/write.

**Tools:**
- `read_text_file` / `read_multiple_files` — Read one or multiple files efficiently
- `write_file` / `edit_file` — Create or edit files with git-style diffs
- `create_directory` — Create directory structures
- `directory_tree` — Get recursive JSON tree view of project structure
- `list_directory` / `list_directory_with_sizes` — List directory contents with optional size info
- `search_files` — Glob-pattern file search across directories
- `move_file` — Move or rename files
- `get_file_info` — Get file metadata (size, timestamps, permissions)

**Use in this project:**
- Scaffold the `src/` directory structure (clients, models, services, routers, websocket)
- Batch-read multiple source files for cross-file analysis
- Search for patterns across the codebase during refactoring
- Monitor file sizes and structure as the project grows

---

#### 3. Memory Server (`github.com/modelcontextprotocol/servers/tree/main/src/memory`)

**Purpose:** Persistent knowledge graph that maintains context across conversation sessions.

**Tools:**
- `create_entities` / `delete_entities` — Manage entities in the knowledge graph
- `create_relations` / `delete_relations` — Manage relationships between entities
- `add_observations` / `delete_observations` — Add or remove facts about entities
- `search_nodes` — Search the graph by query string
- `open_nodes` — Retrieve specific entities by name
- `read_graph` — Read the entire knowledge graph

**Use in this project:**
- **Cross-session continuity** — Architecture decisions, GUID mappings, feature specs, and implementation progress persist between conversations
- **GUID reference** — Quick lookup of Athena enum GUIDs, support group mappings, location hierarchies without re-reading skill.md
- **Implementation tracking** — Track which features/modules are complete, in progress, or pending
- **Decision log** — Record architectural decisions (ADRs) so future sessions don't revisit settled questions

**Current knowledge graph contents:**
- `ServiceDeskHelper` (Project) — Full project description and tech stack
- `AthenaTicketingSystem` (ExternalSystem) — API details, auth, work item types
- `DatabricksBackend` (ExternalSystem) — Endpoints, models, SQL warehouse config
- `OneNoteDocumentationTable` / `IRembeddingsTable` (DatabricksTable) — Schema and statistics
- `Feature1_EnhancedSearch` through `Feature5_TurnoverEmail` (Feature) — Specs, service paths, test paths
- `AthenaEnumMappings` / `AthenaEnumLookupIDs` / `SupportGroupGUIDs` / `AthenaLocationHierarchy` (ReferenceData) — All GUID mappings
- `ArchitectureDecisions` (Decision) — 10 ADRs covering framework, patterns, testing, implementation order
- 28 relations linking all entities

---

#### 4. Browser Tools (`github.com/AgentDeskAI/browser-tools-mcp`)

**Purpose:** Capture browser state for debugging and auditing web applications.

**Tools:**
- `getConsoleLogs` / `getConsoleErrors` — Capture browser console output
- `getNetworkErrors` / `getNetworkLogs` — Monitor network requests and failures
- `takeScreenshot` — Screenshot the current browser tab
- `getSelectedElement` — Inspect a selected DOM element
- `runAccessibilityAudit` — Run accessibility audit (Lighthouse-based)
- `runPerformanceAudit` — Run performance audit
- `runSEOAudit` — Run SEO audit
- `runBestPracticesAudit` — Run best practices audit
- `runDebuggerMode` — Debug application issues
- `wipeLogs` — Clear captured logs

**Use in this project:**
- Debug frontend issues when the web interface is built (console errors, network failures)
- Run accessibility audits to ensure the UI meets WCAG standards
- Run performance audits to identify bottlenecks
- Capture screenshots for visual regression testing
- Monitor API calls from the frontend to the FastAPI backend

---

#### 5. Playwright MCP (`github.com/microsoft/playwright-mcp`)

**Purpose:** Full browser automation for testing and interaction.

**Tools:**
- `browser_navigate` / `browser_navigate_back` — Navigate to URLs
- `browser_click` / `browser_hover` / `browser_drag` — Interact with page elements
- `browser_type` / `browser_fill_form` — Fill in forms and type text
- `browser_select_option` — Select dropdown options
- `browser_snapshot` — Capture accessibility snapshot (better than screenshot for actions)
- `browser_take_screenshot` — Visual screenshot
- `browser_evaluate` / `browser_run_code` — Execute JavaScript or Playwright code
- `browser_console_messages` / `browser_network_requests` — Monitor browser activity
- `browser_tabs` — Manage browser tabs
- `browser_wait_for` — Wait for text or time conditions
- `browser_press_key` — Keyboard input
- `browser_file_upload` — Upload files
- `browser_handle_dialog` — Handle browser dialogs (alerts, confirms, prompts)

**Use in this project:**
- **End-to-end testing** of the web interface when built
- **API testing** via the FastAPI Swagger UI (`/docs`) — navigate, fill forms, submit requests, verify responses
- **Multi-user simulation** for Feature #4 (bulk assignment) — open multiple tabs, simulate concurrent users
- **Visual verification** — screenshot comparison for UI components
- **Athena Swagger exploration** — interact with the Athena test Swagger UI to verify API behavior

### MCP Usage by Development Phase

| Phase | Primary MCP Tools |
|-------|-------------------|
| **Architecture & scaffolding** | Filesystem (directory creation, batch file ops), Memory (store decisions) |
| **Implementation** | Context7 (library docs), Filesystem (file editing), Memory (track progress) |
| **Unit testing** | Context7 (pytest patterns), Filesystem (manage test files) |
| **API integration testing** | Playwright (Swagger UI interaction), Browser Tools (network monitoring) |
| **Frontend development** | Browser Tools (console/network debugging, audits), Playwright (E2E testing) |
| **Cross-session continuity** | Memory (persist project state, decisions, progress between conversations) |
