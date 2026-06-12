---
rule_kind: editFormRule
intent: allow editing only by users whose group matches the group recorded on the
  encounter
entity_param: individual
encounter_types: []
concepts:
- User Group UUID
source_org: JSSCP
---
```js
"use strict";
 ({params, imports}) => {
  const {entity, form, services, entityContext, myUserGroups, userInfo} = params;
 

  const userGroupUUID = entity.getObservationValue('User Group UUID');
 

  let isEditable = true;
 

  if (!userGroupUUID) {
  isEditable = true;
  } else {
  const userGroupUUIDArray = userGroupUUID.split(',').map(s => s.trim());
  const myUserGroupList = myUserGroups
  .filter(grp => grp.groupName != 'Everyone')
  .map(grp => grp.groupUuid);
  const isGroupMatched = myUserGroupList.some(uuid => userGroupUUIDArray.includes(uuid));
  isEditable = isGroupMatched;
  }
 

  const output = {
  editable: {
  value: isEditable,
  messageKey: 'Editing is restricted. This form was submitted by another user group!'
  }
  };
  return output;
 };
```
