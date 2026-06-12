---
rule_kind: editFormRule
intent: allow editing only by the original creator within N days of the registration
entity_param: sameUser
encounter_types: []
concepts: []
source_org: Goonj
---
```js
"use strict";
 ({params, imports}) => {
  const {entity, form, services, entityContext, myUserGroups, user} = params;
  const moment = imports.moment;
  const userGroups = params.myUserGroups
  .map((grp) => grp.groupName);
  const sameUser = params.entity.createdByUUID === params.user.userUUID;
  
  const isUserFieldUser = userGroups.includes('Field Users');
  const isUserFieldSupervisor = userGroups.includes('Field Supervisor');
  
  const isRegistrationDateWithinThreeDays = moment(entity.registrationDate).add(3, 'days').isSameOrAfter(moment(), 'day');
  
  const output = {
  editable : {
  value: (isUserFieldUser || isUserFieldSupervisor) && isRegistrationDateWithinThreeDays && sameUser,
  messageKey: 'Edit access denied:Registration date is past 3 days or You are not allowed to edit other users data'
  } 
  }; 
  return output;
 };
```
