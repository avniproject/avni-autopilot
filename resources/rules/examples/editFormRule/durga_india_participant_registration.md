---
rule_kind: editFormRule
intent: allow editing based on the cohort's approval workflow state (admins always;
  approved blocks non-admins; pending allows only the approver role; rejected re-opens
  editing), and restrict lower roles to records they created
entity_param: individual
encounter_types: []
concepts:
- Created By Username
source_org: Durga India
---
```js
"use strict";
 ({params, imports}) => {
  const {entity, form, services, entityContext, myUserGroups, userInfo} = params;
  console.log('params------>', params);
  const _ = imports.lodash;
  const moment = imports.moment;
  const adminUsers = ['Administrators', "Super Admin"];
  const allUsers = ["Co Coordinator", "Facilitators"]
  const isUserAdmin = _.find(myUserGroups, userGroup => adminUsers.includes(userGroup.groupName));
  const isUserFacilitator = _.find(myUserGroups, userGroup => userGroup.groupName == "Facilitators");
  const isUserCoCoordinator = _.find(myUserGroups, userGroup => userGroup.groupName == "Co Coordinator");
  let eligibility = true;
  
  let message = "";
  const groups = entity.groups || []
 

  if(!isUserAdmin && groups && groups.length > 0) {
  const group = groups[0].groupSubject;
  if(group) {
  const endlineCompleted = group.getEncounters().filter(enc => enc.encounterType.name == 'Submission for approval');
  if(endlineCompleted && endlineCompleted.length > 0) {
  const latestEntityApprovalStatus = endlineCompleted[0].latestEntityApprovalStatus
  if(latestEntityApprovalStatus) {
  const isPending = latestEntityApprovalStatus.isPending;
  const isApproved = latestEntityApprovalStatus.isApproved;
  const isRejected = latestEntityApprovalStatus.isRejected;
 

  if(isPending) {
  if(isUserCoCoordinator) {
  // Do nothing
  eligibility = true;
  } else {
  eligibility = false;
  message = "Edit Denied: The Cohort is Applied for Approval";
  }
  } else if(isApproved) {
  eligibility = false;
  message = "Edit Denied: The Cohort is already Approved";
  } else if(isRejected) {
  // Do nothing
  eligibility = true;
  }
  }
  }
  }
  }
 

  if(isUserAdmin) {
  // Do Nothing
  }
  else if(isUserCoCoordinator) {
  // Do Nothing
  }
  else if(isUserFacilitator) {
  let currentUsername = params.user.username;
  const createdByUsername = entity.getObservationReadableValue("Created By Username");
  if(currentUsername && createdByUsername && currentUsername != createdByUsername) {
  eligibility = false;
  message = "Edit Denied: You are not the creator of this paricipant";
  }
  }
 

  return {
  editable: {
  value: eligibility,
  messageKey: message
  }
  }
 };
```
